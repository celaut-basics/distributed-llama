#!/usr/bin/env python3
"""One benchmark run through the chat interface of a head (chat.md revision 6).

    chatbench.py <ip:port> [--t0 <unix time of the start command>] [--prompt bench/prompt.txt]

It waits until the head answers GetCapabilities, opens `Connect`, sends the fixed prompt and
reads the answer. It prints one JSON object:

- `chat_up_s`: from --t0 to the first answer of GetCapabilities (the instance boots);
- `cold_start_s`: from --t0 to the moment the answer goes IN_PROGRESS (the model is loaded,
  with the workers started and their tensors sent);
- `ttft_s`: from IN_PROGRESS to the first text delta (prompt processing + first token), as the
  client sees it;
- `answer_s`: from IN_PROGRESS to COMPLETED;
- `meta`: the metadata of the answer (llama-server timings, network bytes, boot marks);
- `text`: the answer.
"""

import argparse
import json
import os
import queue
import sys
import threading
import time

import grpc

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "head"))
from app import chat_pb2 as pb  # noqa: E402
from app import chat_pb2_grpc as rpc  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("address")
    parser.add_argument("--t0", type=float, default=None)
    parser.add_argument("--prompt", default=os.path.join(os.path.dirname(__file__), "prompt.txt"))
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--text", default=None, help="send this text instead of the prompt file")
    parser.add_argument("--key", default="bench1", help="the idempotency_key of the send")
    args = parser.parse_args()
    t0 = args.t0 or time.time()
    prompt = args.text if args.text is not None else open(args.prompt).read().strip()
    channel = grpc.insecure_channel(args.address)
    stub = rpc.ChatServiceStub(channel)
    deadline = t0 + args.timeout
    while True:
        try:
            stub.GetCapabilities(pb.GetCapabilitiesRequest(), timeout=5)
            break
        except grpc.RpcError:
            if time.time() > deadline:
                raise SystemExit("the head did not answer")
            time.sleep(0.5)
    t_up = time.time()

    outgoing = queue.Queue()
    events = queue.Queue()

    def requests():
        while True:
            item = outgoing.get()
            if item is None:
                return
            yield item

    call = stub.Connect(requests())

    def read():
        try:
            for event in call:
                events.put((time.time(), event))
        except grpc.RpcError as e:
            events.put((time.time(), e))

    threading.Thread(target=read, daemon=True).start()
    outgoing.put(pb.ClientEvent(subscribe=pb.SubscribeRequest(after_sequence=0), event_id="s"))
    outgoing.put(pb.ClientEvent(event_id="m1", send_message=pb.SendMessageRequest(
        content=[pb.ContentPart(text=pb.TextContent(text=prompt))], idempotency_key=args.key)))
    user_id = answer_id = None
    t_progress = t_first = t_done = None
    status = None
    while time.time() < deadline:
        try:
            t, event = events.get(timeout=1)
        except queue.Empty:
            continue
        if isinstance(event, grpc.RpcError):
            raise SystemExit("the stream failed: %s" % event)
        kind = event.WhichOneof("event")
        if kind == "message_created":
            message = event.message_created.message
            if not message.from_service and message.idempotency_key == args.key:
                user_id = message.message_id
            elif message.from_service and user_id and message.reply_to_message_id == user_id:
                answer_id = message.message_id
        elif kind == "message_status_changed" and event.message_status_changed.message_id == answer_id:
            s = event.message_status_changed.status
            if s == pb.MESSAGE_STATUS_IN_PROGRESS:
                t_progress = t
            elif s >= pb.MESSAGE_STATUS_COMPLETED:
                t_done, status = t, s
                break
        elif kind == "message_delta" and event.message_delta.message_id == answer_id:
            if t_first is None and event.message_delta.text_append:
                t_first = t
        elif kind == "error":
            raise SystemExit("protocol error: %s" % event.error)
    outgoing.put(None)
    messages = list(stub.GetMessages(pb.GetMessagesRequest()))
    answer = [m for m in messages if m.message_id == answer_id][0]
    out = {
        "status": pb.MessageStatus.Name(status) if status is not None else None,
        "error": answer.error if answer.HasField("error") else None,
        "chat_up_s": round(t_up - t0, 3),
        "cold_start_s": round(t_progress - t0, 3) if t_progress else None,
        "ttft_s": round(t_first - t_progress, 3) if t_first and t_progress else None,
        "answer_s": round(t_done - t_progress, 3) if t_done and t_progress else None,
        "t": {"t0": t0, "up": t_up, "progress": t_progress, "first": t_first, "done": t_done},
        "meta": dict(answer.metadata),
        "text": "".join(p.text.text for p in answer.content),
    }
    print(json.dumps(out))
    channel.close()


if __name__ == "__main__":
    main()
