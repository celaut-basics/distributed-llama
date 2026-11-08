"""The calls of nodo's gateway that the head makes: quote, start, fund, watch and stop a
worker, and the reading of ``/__config__``.

Derived from ``nodo_gateway.py`` of celaut-basics/demo-service (branch
``feat/new-benchmarks``). The messages come from ``celaut.binpb``, a copy of nodo's
``protos/celaut_doc.binpb`` (dev ``a97148dd``), loaded into a PRIVATE descriptor pool.
Every gateway RPC is a stream of bee-rpc Buffers each way, so the calls use a generic
stream-stream method and the index tables of nodo's ``protos/gateway_bee.py``. A local
instance is exempt from the Client of the RPCs that ask for one, so no Client is sent.
Since nodo#599 (dev ``515d92aa``), ``StopService`` and ``ModifyDeposit`` take the Client of
the father at index 2, but only when the father is a client: the father of a worker is this
instance, so its token is still the only credential. ``GetMetrics`` with an instance token
is the same as before.
"""
import os
import threading
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
DESCRIPTOR_SET = os.path.join(HERE, "celaut.binpb")
GATEWAY_SERVICE = "/celaut.Gateway/"

_pool_lock = threading.Lock()
_MESSAGES = None


def messages():
    """The message classes of nodo's celaut.proto, from a private descriptor pool."""
    global _MESSAGES
    with _pool_lock:
        if _MESSAGES is not None:
            return _MESSAGES
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
        from bee_rpc import buffer_pb2

        files = descriptor_pb2.FileDescriptorSet()
        with open(DESCRIPTOR_SET, "rb") as fh:
            files.ParseFromString(fh.read())
        pool = descriptor_pool.DescriptorPool()
        # celaut.proto imports buffer.proto: the pool takes bee-rpc's own copy, the one
        # that the Buffers of every call are made of.
        buffer_file = descriptor_pb2.FileDescriptorProto()
        buffer_pb2.DESCRIPTOR.CopyToProto(buffer_file)
        pool.Add(buffer_file)
        for proto in files.file:
            if proto.name != buffer_file.name:
                pool.Add(proto)

        def cls(name):
            return message_factory.GetMessageClass(pool.FindMessageTypeByName("celaut." + name))

        names = ("RecursionGuard", "Configuration", "Metadata", "Service", "StartReference",
                 "ServiceInstance", "TokenMessage", "Refund", "Metrics", "EstimatedCost",
                 "BytesKeyValue", "Amount", "ConfigurationFile", "ModifyDepositInput",
                 "ModifyDepositOutput", "Client")
        m = SimpleNamespace(**{n: cls(n) for n in names})
        m.Hash = cls("Metadata.HashTag.Hash")
        _MESSAGES = m
        return m


def start_service_indices():
    m = messages()
    # protos/gateway_bee.py: StartService_input_indices. GetServiceEstimatedCost takes
    # the same request.
    return {1: m.Client, 2: m.RecursionGuard, 3: m.Configuration, 4: m.Hash,
            5: m.Metadata, 6: m.Service, 7: m.StartReference}


def amount(mu):
    return messages().Amount(n=str(int(mu)))


def mu(amount_message):
    return int(amount_message.n) if amount_message is not None and amount_message.n else 0


class GatewayError(Exception):
    """A gateway RPC failed. ``code`` is the gRPC status name, ``details`` the node's text."""

    def __init__(self, rpc, exc):
        self.rpc = rpc
        code = getattr(exc, "code", None)
        details = getattr(exc, "details", None)
        try:
            self.code = code().name if callable(code) else None
        except Exception:
            self.code = None
        try:
            self.details = details() if callable(details) else None
        except Exception:
            self.details = None
        super().__init__(f"{rpc} failed: {self.code or type(exc).__name__}: "
                         f"{self.details or exc}")


def _method(channel, rpc):
    from bee_rpc import buffer_pb2
    return channel.stream_stream(
        GATEWAY_SERVICE + rpc,
        request_serializer=buffer_pb2.Buffer.SerializeToString,
        response_deserializer=buffer_pb2.Buffer.FromString,
    )


def call_one(node_url, rpc, request, in_indices, out_indices, timeout=30, block_skip=False):
    """One request message (or an iterator of them), the first answer message."""
    import grpc
    from bee_rpc.client import client_grpc
    channel = grpc.insecure_channel(node_url)
    try:
        return next(client_grpc(
            method=_method(channel, rpc),
            block_skip=block_skip,
            input=request,
            indices_serializer=in_indices,
            indices_parser=out_indices,
            partitions_message_mode_parser=True,
            timeout=timeout,
        ), None)
    except grpc.RpcError as e:
        raise GatewayError(rpc, e) from e
    finally:
        channel.close()


class PackedService:
    """A dependency that the packer put in this service's image.

    ``<dir>/__services__/<id>`` holds the Service (a bee-rpc directory: its parts and its
    blocks by reference) and ``<dir>/__metadata__/<id>`` its Metadata.
    """

    def __init__(self, service_id, root):
        self.id = service_id
        self.service_path = os.path.join(root, "__services__", service_id)
        self.metadata_path = os.path.join(root, "__metadata__", service_id)

    def metadata(self):
        m = messages().Metadata()
        with open(self.metadata_path, "rb") as fh:
            m.ParseFromString(fh.read())
        return m

    def request(self, config=None):
        """The StartService (or GetServiceEstimatedCost) request, in nodo's order.

        The Configuration comes before the hashes: the node may begin the start at the
        first hash that it holds, and a field that arrives after that is ignored.
        """
        from bee_rpc.client import Dir
        m = messages()
        if config is not None:
            yield config
        metadata = self.metadata()
        for h in metadata.hashtag.hash:
            yield m.Hash(type=h.type, value=h.value)
        yield metadata
        yield Dir(dir=self.service_path, _type=m.Service)


def configuration(initial_mu=None, envs=None):
    m = messages()
    config = m.Configuration()
    if initial_mu is not None:
        config.initial_mu.CopyFrom(amount(initial_mu))
    for key, value in (envs or {}).items():
        config.environment_variables.append(
            m.BytesKeyValue(key=key, value=value if isinstance(value, bytes) else value.encode()))
    return config


def start_service(node_url, packed, config=None, timeout=900):
    """StartService for ``packed``: the ServiceInstance. The block skip is on, so a node
    that holds the service (or its blocks) stops the sender early."""
    m = messages()
    instance = call_one(node_url, "StartService", packed.request(config=config),
                        start_service_indices(), {1: m.ServiceInstance}, timeout=timeout,
                        block_skip=True)
    if instance is None:
        raise GatewayError("StartService", RuntimeError("the node answered no ServiceInstance"))
    return instance


def estimated_cost(node_url, packed, config=None, timeout=300):
    m = messages()
    cost = call_one(node_url, "GetServiceEstimatedCost", packed.request(config=config),
                    start_service_indices(), {1: m.EstimatedCost}, timeout=timeout,
                    block_skip=True)
    if cost is None:
        raise GatewayError("GetServiceEstimatedCost", RuntimeError("no EstimatedCost"))
    return cost


def stop_service(node_url, token, timeout=60):
    m = messages()
    refund = call_one(node_url, "StopService", m.TokenMessage(token=token),
                      {1: m.TokenMessage}, {1: m.Refund}, timeout=timeout)
    return mu(refund.amount) if refund is not None else 0


def metrics_balance(node_url, token, timeout=30):
    m = messages()
    out = call_one(node_url, "GetMetrics", m.TokenMessage(token=token),
                   {1: m.TokenMessage}, {1: m.Metrics}, timeout=timeout)
    return mu(out.balance) if out is not None else None


def modify_deposit(node_url, token, difference_mu, timeout=30):
    """Move ``difference_mu`` from this instance to its child ``token`` (negative: back)."""
    m = messages()
    out = call_one(node_url, "ModifyDeposit",
                   m.ModifyDepositInput(difference=amount(difference_mu), service_token=token),
                   {1: m.ModifyDepositInput}, {1: m.ModifyDepositOutput}, timeout=timeout)
    return out


def read_config(path=None):
    """(gateway "ip:port", the ConfigurationFile) of this instance. The entrypoint gives
    the service user a copy of /__config__ in CELAUT_CONFIG."""
    path = path or os.environ.get("CELAUT_CONFIG") or "/__config__"
    m = messages()
    cfg = m.ConfigurationFile()
    with open(path, "rb") as fh:
        cfg.ParseFromString(fh.read())
    for slot in cfg.gateway.uri_slot:
        for uri in slot.uri:
            return f"{uri.ip}:{uri.port}", cfg
    raise RuntimeError("the configuration file names no gateway address")


def slot_address(instance, port):
    """The first "ip:port" where the caller reaches ``port`` of a ServiceInstance."""
    for slot in instance.instance.uri_slot:
        if slot.internal_port == port:
            for uri in slot.uri:
                return f"{uri.ip}:{uri.port}"
    return None
