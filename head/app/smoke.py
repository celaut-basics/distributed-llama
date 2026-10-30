"""Build-time smoke test of the head (head/runtime.Dockerfile): with LLAMA_WORKERS=0, the
model loads and one message gets a COMPLETED answer through the chat interface."""

import sys
import time

import grpc

from . import chat_pb2 as pb
from . import chat_pb2_grpc as rpc
from .chat import Chat
from .cluster import Cluster
from .llm import Backend
from .server import make_server


def main():
    cluster = Cluster()
    assert cluster.want == 0, "the smoke test runs with LLAMA_WORKERS=0"
    chat = Chat(Backend(cluster))
    server, port = make_server(chat, host="127.0.0.1", port=0)
    server.start()
    cluster.start()
    stub = rpc.ChatServiceStub(grpc.insecure_channel("127.0.0.1:%d" % port))
    caps = stub.GetCapabilities(pb.GetCapabilitiesRequest())
    assert caps.supports_cancel and not caps.accepted_mime_types, caps
    stub.SendMessage(pb.SendMessageRequest(
        content=[pb.ContentPart(text=pb.TextContent(text="Say hello."))], idempotency_key="smoke"))
    deadline = time.time() + 300
    while time.time() < deadline:
        messages = list(stub.GetMessages(pb.GetMessagesRequest()))
        answer = messages[1]
        if answer.status not in (pb.MESSAGE_STATUS_PENDING, pb.MESSAGE_STATUS_IN_PROGRESS):
            break
        time.sleep(0.5)
    text = "".join(p.text.text for p in answer.content)
    assert answer.status == pb.MESSAGE_STATUS_COMPLETED and text, answer
    print("smoke test: %r, %s tok/s" % (text, answer.metadata.get("gen_tps")))
    cluster.stop()
    server.stop(0)
    sys.exit(0)


if __name__ == "__main__":
    main()
