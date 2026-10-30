"""The gRPC server of the chat interface (chat.md revision 6), h2c on the port of the slot.

The structure is the one of celaut-basics/echo-chat (branch chat-rev6, service/server.py).
The service accepts text only, so `FileService` is not implemented: each of its calls ends
with UNIMPLEMENTED (chat.md section 6.4), as each call that chat.proto does not define.
"""

import queue
import re
import threading
from concurrent import futures

import grpc

from . import chat_pb2 as pb
from . import chat_pb2_grpc as rpc
from .chat import Reject, bad_request

EVENT_ID = re.compile("[A-Za-z0-9_-]{1,64}")
END = object()
BAD_EVENT_ID = bad_request("event_id is not valid or is not new")


def fail(context, error):
    """End the call with the status and the trailer `chat-reason` (section 9)."""
    context.set_trailing_metadata((("chat-reason", error.reason),))
    context.abort(getattr(grpc.StatusCode, error.status), error.message)


def protocol_error(error, event_id):
    return pb.ServerEvent(error=pb.ProtocolError(
        code=error.reason, message=error.message, event_id=event_id))


class ChatService(rpc.ChatServiceServicer):
    def __init__(self, chat):
        self.chat = chat

    def _call(self, context, function, *args):
        try:
            return function(*args)
        except Reject as error:
            fail(context, error)

    def GetCapabilities(self, request, context):
        return self.chat.capabilities()

    def GetConversation(self, request, context):
        if request.conversation_id:
            fail(context, Reject("NOT_FOUND", "not_found", "there is only the main conversation"))
        return self.chat.conversation()

    def ForkConversation(self, request, context):
        fail(context, Reject("UNIMPLEMENTED", "unimplemented", "supports_fork is false"))

    def SendMessage(self, request, context):
        return self._call(context, self.chat.send, request)[0]

    def GetMessages(self, request, context):
        yield from self._call(context, self.chat.get_messages, request)

    def SetMessagePinned(self, request, context):
        return self._call(context, self.chat.set_pinned, request)

    def Connect(self, request_iterator, context):
        events = queue.Queue()
        context.add_callback(lambda: events.put(END))  # the client went away
        seen = set()  # the event_id of each event, to find a repeat
        state = {"subscribed": False}

        def end(error):
            context.set_trailing_metadata((("chat-reason", error.reason),))
            context.set_code(getattr(grpc.StatusCode, error.status))
            context.set_details(error.message)
            events.put(END)

        def handle(event):
            kind = event.WhichOneof("event")
            if not state["subscribed"] and kind != "subscribe":
                raise Reject("FAILED_PRECONDITION", "bad_state", "the first event is not subscribe")
            if event.event_id:
                if not EVENT_ID.fullmatch(event.event_id) or event.event_id in seen:
                    raise BAD_EVENT_ID
                seen.add(event.event_id)
            if kind == "subscribe":
                if state["subscribed"]:
                    raise Reject("FAILED_PRECONDITION", "bad_state", "the stream has a subscribe")
                if event.subscribe.conversation_id:
                    raise Reject("NOT_FOUND", "not_found", "there is only the main conversation")
                self.chat.subscribe(events, event.subscribe.after_sequence)
                state["subscribed"] = True
            elif kind == "send_message":
                message, repeat = self.chat.send(event.send_message)
                if repeat:
                    self.chat.repeat(events, message)
            elif kind == "acknowledge":
                # A hint: the service keeps no events for a stream, so it drops none.
                self.chat.check_acknowledge(event.acknowledge.last_sequence)
            elif kind == "cancel_message":
                self.chat.cancel(event.cancel_message)
            # Another event is of a newer revision: ignore it.

        def read():
            try:
                for event in request_iterator:
                    try:
                        handle(event)
                    except Reject as error:
                        if not state["subscribed"]:  # section 6.5, steps 1 and 2
                            return end(error)
                        # A bad or repeated event_id is not copied (section 6.5).
                        echo = event.event_id if event.event_id and error is not BAD_EVENT_ID else None
                        events.put(protocol_error(error, echo))
            except grpc.RpcError:
                pass
            events.put(END)  # the client ended its side: end with OK

        threading.Thread(target=read, daemon=True).start()
        try:
            while True:
                event = events.get()
                if event is END:
                    return
                yield event
        finally:
            self.chat.unsubscribe(events)


class Unimplemented(grpc.GenericRpcHandler):
    """A call that the service does not implement (FileService, or a call that chat.proto
    does not define) ends with UNIMPLEMENTED and its reason."""

    def service(self, details):
        def handler(request, context):
            fail(context, Reject("UNIMPLEMENTED", "unimplemented", "the call is not implemented"))
        return grpc.unary_unary_rpc_method_handler(handler)


def make_server(chat, host="0.0.0.0", port=8080):
    """Return (server, port). The server is not started."""
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=64),
        options=(("grpc.so_reuseport", 0),
                 ("grpc.max_receive_message_length", 4 << 20),
                 ("grpc.max_send_message_length", 1 << 20),
                 ("grpc.max_concurrent_streams", 100),
                 ("grpc.http2.max_ping_strikes", 0),  # never close for a PING
                 ("grpc.keepalive_permit_without_calls", 1)))
    rpc.add_ChatServiceServicer_to_server(ChatService(chat), server)
    server.add_generic_rpc_handlers((Unimplemented(),))  # after the service
    return server, server.add_insecure_port("%s:%d" % (host, port))
