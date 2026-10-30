"""The conversation of the head: the history and the answers of the model.

chat.md revision 6 (celaut-project/chatui, docs/celaut/chat.md). The structure is the one
of celaut-basics/echo-chat (branch chat-rev6, service/store.py): everything that changes
the history, and the event that tells about the change, happen under one lock. So a
`Connect` stream that joins sees a replay and then the live events, with none lost and
none sent twice (section 6.5, step 3).

The differences from the echo: the answer comes from the model (`llm.Backend`), as a
stream of deltas; the service accepts text only (`accepted_mime_types` is empty, so
`FileService` is not implemented, section 6.4); it implements `cancel_message`; a failed
answer has `Message.error` (field 14).
"""

import hashlib
import os
import queue
import re
import threading
import time
import uuid

from . import chat_pb2 as pb

SHA3_256_TYPE = hashlib.sha3_256(b"").digest()  # chat.md section 6.4

MAX_TEXT = 16_384
MAX_REQUEST = 128 << 10
MAX_FILE = 4 << 20
MAX_FILES = 4
READ_LIMIT = 1000  # the most messages of one GetMessages
MAX_ERROR = 1024
KEY = re.compile("[A-Za-z0-9_-]{1,64}")
STATUS = pb.MessageStatus
OPEN = (STATUS.MESSAGE_STATUS_PENDING, STATUS.MESSAGE_STATUS_IN_PROGRESS)
# A delta has 64 KiB or less (section 5.2); the model makes far less in one step.
MAX_DELTA = 60_000


class Reject(Exception):
    """A call that fails. `status` is a name of `grpc.StatusCode`, `reason` goes in the
    trailer `chat-reason` (chat.md section 9)."""

    def __init__(self, status, reason, message=""):
        super().__init__(message or reason)
        self.status, self.reason, self.message = status, reason, message or reason


def bad_request(message):
    return Reject("INVALID_ARGUMENT", "bad_request", message)


def normalize_mime(mime):
    return mime.split(";")[0].strip().lower()


def text_of(content):
    return "".join(p.text.text for p in content if p.WhichOneof("kind") == "text")


def cut_error(text):
    """`error` is UTF-8 of 1 to 1 024 bytes, and no character is cut."""
    data = (text or "the answer failed").encode()[:MAX_ERROR]
    return data.decode("utf-8", "ignore") or "the answer failed"


class Chat:
    """One main conversation. `backend` makes the answers (see `llm.Backend`)."""

    def __init__(self, backend, max_messages=None, max_queue=None):
        env = os.environ.get
        self.backend = backend
        self.max_messages = int(env("CHAT_MAX_MESSAGES", 10_000)) if max_messages is None else max_messages
        self.max_queue = int(env("CHAT_MAX_QUEUE", 16)) if max_queue is None else max_queue
        self.created_at = self._now()
        self.messages = []  # sequence n is messages[n - 1]
        self.by_id = {}
        self.keys = {}  # idempotency_key -> message_id
        self.subscribers = []
        self.pending = 0  # user messages that wait for an answer
        self.cancelled = set()  # message_id of the answers that the client cancelled
        self.lock = threading.Lock()
        self.jobs = queue.Queue()
        self.worker = threading.Thread(target=self._work, daemon=True)
        self.worker.start()

    def close(self):
        self.jobs.put(None)

    @staticmethod
    def _now():
        return time.time_ns() // 1_000_000

    @property
    def latest(self):
        return len(self.messages)

    # The next methods run with the lock held.

    def _touch(self, message):
        message.updated_at = max(self._now(), message.updated_at)

    def _publish(self, **event):
        event = pb.ServerEvent(**event)
        for q in self.subscribers:
            q.put(event)

    def _add(self, **fields):
        now = self._now()
        message = pb.Message(message_id=uuid.uuid4().hex, sequence=self.latest + 1,
                             created_at=now, updated_at=now, **fields)
        self.messages.append(message)
        self.by_id[message.message_id] = message
        self._publish(message_created=pb.MessageCreated(message=message))
        return message

    def _set_status(self, message, status, error=None):
        message.status = status
        if error is not None:
            message.error = error
        self._touch(message)
        self._publish(message_status_changed=pb.MessageStatusChanged(
            message_id=message.message_id, status=status, updated_at=message.updated_at,
            error=error))

    def _pin(self, message, pinned):
        if message.pinned == pinned:
            return
        message.pinned = pinned
        self._touch(message)
        self._publish(message_pinned_changed=pb.MessagePinnedChanged(
            message_id=message.message_id, pinned=pinned, updated_at=message.updated_at))

    # Calls.

    def capabilities(self):
        # Text only (accepted_mime_types empty), all limits the defaults of chat.md.
        return pb.Capabilities(accepted_mime_types=[], supports_fork=False, supports_cancel=True)

    def conversation(self):
        return pb.Conversation(conversation_id="", created_at=self.created_at, archived=False)

    def send(self, request):
        """chat.md section 6.1. Return (user message, True if it is a repeat)."""
        if request.conversation_id:
            raise Reject("NOT_FOUND", "not_found", "there is only the main conversation")
        # Step 1: the structure.
        if not request.content:
            raise bad_request("content has no part")
        attachments = []
        for part in request.content:
            kind = part.WhichOneof("kind")
            if kind not in ("text", "image", "audio"):
                raise bad_request("a part needs a kind that a client can send")
            if kind != "text":
                attachments.append((kind, getattr(part, kind).file))
        if request.idempotency_key and not KEY.fullmatch(request.idempotency_key):
            raise bad_request("idempotency_key has 1 to 64 characters of A-Za-z0-9_-")
        text = text_of(request.content)
        if not text and not attachments:
            raise bad_request("the text is empty and there is no attachment")
        for kind, ref in attachments:
            if not ref.hashes or any(not h.type for h in ref.hashes):
                raise bad_request("each hash of a file needs a type")
            if not ref.mime_type or ref.size_bytes < 1:
                raise bad_request("a file needs mime_type and size_bytes of 1 or more")
            mime = normalize_mime(ref.mime_type)
            if (mime.startswith("image/") and kind != "image") or (
                    mime.startswith("audio/") and kind != "audio"):
                raise bad_request("the kind of the part does not agree with mime_type")
        if len(attachments) > MAX_FILES:
            raise bad_request("a message has %d attachments or less" % MAX_FILES)
        # Step 2: the size.
        if (len(text.encode()) > MAX_TEXT or request.ByteSize() > MAX_REQUEST
                or any(ref.size_bytes > MAX_FILE for _, ref in attachments)):
            raise Reject("INVALID_ARGUMENT", "too_large", "the text, the request or a file is too large")
        # Step 3: the type. The service accepts text only.
        if attachments:
            raise Reject("INVALID_ARGUMENT", "unsupported_media", "the service accepts text only")
        with self.lock:
            # Step 4.
            if request.reply_to_message_id and request.reply_to_message_id not in self.by_id:
                raise Reject("NOT_FOUND", "not_found", "reply_to_message_id is not a message")
            # Step 5 has nothing to check: there is no attachment.
            # Step 6.
            if request.idempotency_key and request.idempotency_key in self.keys:
                known = self.by_id[self.keys[request.idempotency_key]]
                return pb.Message.FromString(known.SerializeToString()), True
            # Steps 7 and 8. Each message that waits adds its answer.
            if self.latest + self.pending + 2 > self.max_messages:
                raise Reject("RESOURCE_EXHAUSTED", "history_full", "start a new instance")
            if self.pending >= self.max_queue:
                raise Reject("UNAVAILABLE", "busy", "try again later")
            content = [pb.ContentPart.FromString(p.SerializeToString()) for p in request.content]
            user = self._add(content=content, status=STATUS.MESSAGE_STATUS_COMPLETED,
                             reply_to_message_id=request.reply_to_message_id or None,
                             idempotency_key=request.idempotency_key or None)
            self.pending += 1
            if user.idempotency_key:
                self.keys[user.idempotency_key] = user.message_id
            # The answer is PENDING from the send: the model makes one answer at a time,
            # and the first one can wait for the workers to start.
            answer = self._add(status=STATUS.MESSAGE_STATUS_PENDING, from_service=True,
                               reply_to_message_id=user.message_id)
            self.jobs.put({"user": user.message_id, "answer": answer.message_id})
            return pb.Message.FromString(user.SerializeToString()), False

    def get_messages(self, request):
        """chat.md section 6.2. Return a list of messages."""
        if request.conversation_id:
            raise Reject("NOT_FOUND", "not_found", "there is only the main conversation")
        if request.limit and request.last:
            raise bad_request("limit and last cannot go together")
        with self.lock:
            if request.after_sequence > self.latest:
                raise bad_request("after_sequence is more than latest")
            first = request.after_sequence + 1
            end = self.latest if not request.before_sequence else min(self.latest, request.before_sequence - 1)
            if end < first:
                return []
            if request.last:
                first = max(first, end - min(request.last, READ_LIMIT) + 1)
            else:
                end = min(end, first + (request.limit or READ_LIMIT) - 1, first + READ_LIMIT - 1)
            return [pb.Message.FromString(m.SerializeToString()) for m in self.messages[first - 1:end]]

    def set_pinned(self, request):
        if request.conversation_id:
            raise Reject("NOT_FOUND", "not_found", "there is only the main conversation")
        with self.lock:
            message = self.by_id.get(request.message_id)
            if message is None:
                raise Reject("NOT_FOUND", "not_found", "there is no such message")
            self._pin(message, request.pinned)
            return pb.Message.FromString(message.SerializeToString())

    def cancel(self, request):
        """`cancel_message` of `Connect` (section 6.5)."""
        if request.conversation_id:
            raise Reject("NOT_FOUND", "not_found", "there is only the main conversation")
        with self.lock:
            message = self.by_id.get(request.message_id)
            if message is None:
                raise Reject("NOT_FOUND", "not_found", "there is no such message")
            if message.status not in OPEN:
                raise Reject("FAILED_PRECONDITION", "bad_state", "the message is final")
            # The answer stops at the next token; the status is final now.
            self.cancelled.add(message.message_id)
            self._set_status(message, STATUS.MESSAGE_STATUS_CANCELLED)

    def subscribe(self, events, after_sequence):
        """Put the replay and `SubscriptionReady` in `events`, then the live events."""
        with self.lock:
            if after_sequence > self.latest:
                raise bad_request("after_sequence is more than latest")
            for message in self.messages[after_sequence:]:
                events.put(pb.ServerEvent(message_created=pb.MessageCreated(message=message)))
            events.put(pb.ServerEvent(subscription_ready=pb.SubscriptionReady(
                latest_sequence=self.latest)))
            self.subscribers.append(events)

    def unsubscribe(self, events):
        with self.lock:
            if events in self.subscribers:
                self.subscribers.remove(events)

    def repeat(self, events, message):
        """The `MessageCreated` of a send that was a repeat goes to the stream of the repeat."""
        events.put(pb.ServerEvent(message_created=pb.MessageCreated(message=message)))

    def check_acknowledge(self, last_sequence):
        with self.lock:
            if last_sequence > self.latest:
                raise bad_request("last_sequence is more than latest")

    # The answers.

    def _history(self, user_id):
        """The turns of the model up to the user message `user_id`: final answers and
        user texts, in order of sequence."""
        turns = []
        with self.lock:
            last = self.by_id[user_id].sequence
            for m in self.messages[:last]:
                if not m.from_service:
                    turns.append(("user", text_of(m.content)))
                elif m.status == STATUS.MESSAGE_STATUS_COMPLETED:
                    turns.append(("assistant", text_of(m.content)))
        return turns

    def _work(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                self._answer(job)
            except Exception as error:  # an answer must end (section 5.2)
                self._fail(job["answer"], "the service failed: %s" % (error,))
            finally:
                with self.lock:
                    self.pending -= 1
                    self.cancelled.discard(job["answer"])

    def _fail(self, answer_id, error):
        with self.lock:
            answer = self.by_id[answer_id]
            if answer.status in OPEN:
                self._set_status(answer, STATUS.MESSAGE_STATUS_FAILED, cut_error(error))

    def _is_cancelled(self, answer_id):
        with self.lock:
            return answer_id in self.cancelled

    def _start(self, answer_id):
        """PENDING to IN_PROGRESS. False if the client cancelled the answer."""
        with self.lock:
            answer = self.by_id[answer_id]
            if answer.status != STATUS.MESSAGE_STATUS_PENDING:
                return False
            self._set_status(answer, STATUS.MESSAGE_STATUS_IN_PROGRESS)
            return True

    def _delta(self, answer_id, text="", reasoning=""):
        while text or reasoning:
            t, text = _split_utf8(text, MAX_DELTA)
            r, reasoning = _split_utf8(reasoning, MAX_DELTA)
            with self.lock:
                answer = self.by_id[answer_id]
                if answer.status != STATUS.MESSAGE_STATUS_IN_PROGRESS:
                    return
                if t:
                    if answer.content and answer.content[-1].WhichOneof("kind") == "text":
                        answer.content[-1].text.text += t
                    else:
                        answer.content.add().text.text = t
                answer.reasoning += r
                self._publish(message_delta=pb.MessageDelta(
                    message_id=answer_id, text_append=t, reasoning_append=r))

    def _answer(self, job):
        answer_id = job["answer"]
        # The model may still load (the workers start at the start of the instance).
        if not self.backend.wait_ready(cancelled=lambda: self._is_cancelled(answer_id)):
            if not self._is_cancelled(answer_id):
                self._fail(answer_id, self.backend.not_ready_reason())
            return
        if not self._start(answer_id):
            return
        notice = self.backend.take_notice()
        if notice:
            self._delta(answer_id, text=notice + "\n\n")
        result = self.backend.generate(
            self._history(job["user"]),
            on_text=lambda t: self._delta(answer_id, text=t),
            on_reasoning=lambda r: self._delta(answer_id, reasoning=r),
            cancelled=lambda: self._is_cancelled(answer_id))
        with self.lock:
            answer = self.by_id[answer_id]
            if answer.status != STATUS.MESSAGE_STATUS_IN_PROGRESS:
                return  # cancelled: a final message does not change
            # The measures of the answer (the client ignores metadata, section 5).
            for key, value in result.metadata.items():
                answer.metadata[key] = str(value)
            if result.error:
                self._set_status(answer, STATUS.MESSAGE_STATUS_FAILED, cut_error(result.error))
            else:
                self._set_status(answer, STATUS.MESSAGE_STATUS_COMPLETED)


def _split_utf8(text, limit):
    """(head, rest): head has `limit` UTF-8 bytes or less, and no character is cut."""
    if len(text.encode()) <= limit:
        return text, ""
    head = text.encode()[:limit].decode("utf-8", "ignore")
    return head, text[len(head):]
