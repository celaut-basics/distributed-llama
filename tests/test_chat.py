"""The chat interface of the head (chat.md revision 6) with a fake model: no llama.cpp, no node.

Run from the repository root: `python -m pytest tests` (requirements-dev.txt).
"""

import os
import queue
import sys
import threading
import time
from types import SimpleNamespace

import grpc
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "head"))

from app import chat_pb2 as pb  # noqa: E402
from app import chat_pb2_grpc as rpc  # noqa: E402
from app.chat import Chat  # noqa: E402
from app.llm import build_messages  # noqa: E402
from app.server import make_server  # noqa: E402


class FakeBackend:
    """Answers with the words of the last user text, one delta each 20 ms."""

    def __init__(self):
        self.ready = threading.Event()
        self.ready.set()
        self.notice = None
        self.turns = []

    def wait_ready(self, cancelled):
        while not self.ready.wait(0.05):
            if cancelled():
                return False
        return True

    def not_ready_reason(self):
        return "not ready"

    def take_notice(self):
        notice, self.notice = self.notice, None
        return notice

    def generate(self, turns, on_text, on_reasoning, cancelled):
        self.turns.append(turns)
        text = turns[-1][1]
        if text == "fail":
            on_text("half")
            return SimpleNamespace(error="the worker stopped", metadata={"mode": "distributed"})
        for word in text.split():
            if cancelled():
                break
            on_text(word + " ")
            time.sleep(0.02)
        return SimpleNamespace(error=None, metadata={"gen_tps": 12.5, "mode": "local"})


@pytest.fixture()
def service():
    backend = FakeBackend()
    chat = Chat(backend)
    server, port = make_server(chat, host="127.0.0.1", port=0)
    server.start()
    channel = grpc.insecure_channel("127.0.0.1:%d" % port)
    yield SimpleNamespace(stub=rpc.ChatServiceStub(channel), chat=chat, backend=backend,
                          channel=channel)
    channel.close()
    server.stop(0)
    chat.close()


def text_request(text, key=None):
    return pb.SendMessageRequest(content=[pb.ContentPart(text=pb.TextContent(text=text))],
                                 idempotency_key=key)


def wait_final(stub, n=2, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        messages = list(stub.GetMessages(pb.GetMessagesRequest()))
        if len(messages) >= n and all(m.status >= pb.MESSAGE_STATUS_COMPLETED for m in messages):
            return messages
        time.sleep(0.05)
    raise AssertionError("no final answer: %s" % messages)


def reason(error):
    return dict(error.trailing_metadata() or ()).get("chat-reason")


def test_capabilities_text_only_with_cancel(service):
    caps = service.stub.GetCapabilities(pb.GetCapabilitiesRequest())
    assert list(caps.accepted_mime_types) == []
    assert caps.supports_cancel and not caps.supports_fork


def test_send_and_answer(service):
    user = service.stub.SendMessage(text_request("hello there", "k1"))
    assert user.sequence == 1 and user.idempotency_key == "k1" and not user.from_service
    messages = wait_final(service.stub)
    answer = messages[1]
    assert answer.from_service and answer.reply_to_message_id == user.message_id
    assert answer.status == pb.MESSAGE_STATUS_COMPLETED
    assert "".join(p.text.text for p in answer.content) == "hello there "
    assert answer.metadata["gen_tps"] == "12.5"
    assert not answer.HasField("error")


def test_repeat_key_adds_nothing(service):
    first = service.stub.SendMessage(text_request("a", "same"))
    again = service.stub.SendMessage(text_request("b", "same"))
    assert again.message_id == first.message_id
    wait_final(service.stub)
    assert len(list(service.stub.GetMessages(pb.GetMessagesRequest()))) == 2


def test_attachment_is_unsupported_media(service):
    ref = pb.FileReference(hashes=[pb.Hash(type=b"\x01" * 32, value=b"\x02" * 32)],
                           mime_type="image/png", size_bytes=10)
    request = pb.SendMessageRequest(content=[
        pb.ContentPart(text=pb.TextContent(text="look")),
        pb.ContentPart(image=pb.ImageContent(file=ref))])
    with pytest.raises(grpc.RpcError) as e:
        service.stub.SendMessage(request)
    assert e.value.code() == grpc.StatusCode.INVALID_ARGUMENT and reason(e.value) == "unsupported_media"


def test_empty_text_is_bad_request(service):
    with pytest.raises(grpc.RpcError) as e:
        service.stub.SendMessage(text_request(""))
    assert reason(e.value) == "bad_request"


def test_failed_answer_has_error(service):
    service.stub.SendMessage(text_request("fail"))
    answer = wait_final(service.stub)[1]
    assert answer.status == pb.MESSAGE_STATUS_FAILED
    assert answer.error == "the worker stopped"
    assert "".join(p.text.text for p in answer.content) == "half"


def test_notice_is_at_the_start_of_the_next_answer(service):
    service.backend.notice = "*(worker 2 stopped)*"
    service.stub.SendMessage(text_request("ok"))
    answer = wait_final(service.stub)[1]
    assert "".join(p.text.text for p in answer.content) == "*(worker 2 stopped)*\n\nok "


def test_history_is_given_to_the_model(service):
    service.stub.SendMessage(text_request("one"))
    wait_final(service.stub, 2)
    service.stub.SendMessage(text_request("two"))
    wait_final(service.stub, 4)
    assert service.backend.turns[-1] == [("user", "one"), ("assistant", "one "), ("user", "two")]


def connect(stub, events):
    outgoing = queue.Queue()

    def requests():
        while True:
            item = outgoing.get()
            if item is None:
                return
            yield item

    call = stub.Connect(requests())
    threading.Thread(target=lambda: [events.put(e) for e in call], daemon=True).start()
    return outgoing, call


def next_kind(events, kind, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = events.get(timeout=0.1)
        except queue.Empty:
            continue
        if event.WhichOneof("event") == kind:
            return event
    raise AssertionError("no %s" % kind)


def test_connect_stream_and_cancel(service):
    events = queue.Queue()
    out, call = connect(service.stub, events)
    out.put(pb.ClientEvent(subscribe=pb.SubscribeRequest(after_sequence=0), event_id="s"))
    assert next_kind(events, "subscription_ready").subscription_ready.latest_sequence == 0
    words = " ".join("w%d" % i for i in range(200))
    out.put(pb.ClientEvent(send_message=text_request(words, "c1"), event_id="e1"))
    user = next_kind(events, "message_created").message_created.message
    assert user.idempotency_key == "c1"
    answer = next_kind(events, "message_created").message_created.message
    assert answer.status == pb.MESSAGE_STATUS_PENDING
    next_kind(events, "message_delta")
    out.put(pb.ClientEvent(cancel_message=pb.CancelMessageRequest(message_id=answer.message_id),
                           event_id="e2"))
    deadline = time.time() + 5
    while True:
        event = next_kind(events, "message_status_changed")
        if event.message_status_changed.status == pb.MESSAGE_STATUS_CANCELLED:
            break
        assert time.time() < deadline
    final = wait_final(service.stub)[1]
    assert final.status == pb.MESSAGE_STATUS_CANCELLED
    # A second cancel of a final message: ProtocolError bad_state with the event_id.
    out.put(pb.ClientEvent(cancel_message=pb.CancelMessageRequest(message_id=answer.message_id),
                           event_id="e3"))
    error = next_kind(events, "error").error
    assert error.code == "bad_state" and error.event_id == "e3"
    # A repeated event_id is not copied.
    out.put(pb.ClientEvent(acknowledge=pb.AcknowledgeEvent(last_sequence=1), event_id="e3"))
    error = next_kind(events, "error").error
    assert error.code == "bad_request" and not error.event_id
    out.put(None)


def test_first_event_must_be_subscribe(service):
    call = service.stub.Connect(iter([pb.ClientEvent(acknowledge=pb.AcknowledgeEvent(last_sequence=0))]))
    with pytest.raises(grpc.RpcError) as e:
        list(call)
    assert e.value.code() == grpc.StatusCode.FAILED_PRECONDITION and reason(e.value) == "bad_state"


def test_file_service_is_unimplemented(service):
    method = service.channel.unary_unary("/FileService/DownloadFile",
                                         request_serializer=lambda x: x,
                                         response_deserializer=lambda x: x)
    with pytest.raises(grpc.RpcError) as e:
        method(b"", timeout=5)
    assert e.value.code() == grpc.StatusCode.UNIMPLEMENTED and reason(e.value) == "unimplemented"


def test_answer_secret_request_finds_no_request(service):
    # The service sends no secret request (chat.md section 6.9, step 1; test 80).
    request = pb.AnswerSecretRequestRequest(message_id="m", request_id="r", value="test-value")
    with pytest.raises(grpc.RpcError) as e:
        service.stub.AnswerSecretRequest(request, timeout=5)
    assert e.value.code() == grpc.StatusCode.NOT_FOUND and reason(e.value) == "not_found"
    assert "test-value" not in (e.value.details() or "")


def test_build_messages_keeps_the_last_turns():
    turns = [("user", "x" * 9000), ("assistant", "y" * 9000), ("user", "z")]
    messages = build_messages(turns)
    assert messages[0]["role"] == "system"
    assert [m["content"][0] for m in messages[1:]] == ["y", "z"]
