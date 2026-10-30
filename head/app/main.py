"""The head of distributed-llama: the chat interface of chatui (chat.md revision 6) on the
port of the slot, answered by llama.cpp with its layers split across worker instances.

Run as `python3 -m app.main` from the directory that holds `app/`.
"""

import logging
import os
import signal
import sys
import threading

from .chat import Chat
from .cluster import Cluster
from .llm import Backend
from .server import make_server


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cluster = Cluster()
    chat = Chat(Backend(cluster))
    # The chat answers at once; the answers wait (PENDING) until the model is loaded.
    server, port = make_server(chat, port=int(os.environ.get("PORT", "8080")))
    server.start()
    logging.info("distributed-llama head: gRPC on port %d, %d worker(s) wanted", port, cluster.want)
    cluster.start()

    done = threading.Event()

    def stop(signum, frame):
        logging.info("signal %d: stop the workers and the model", signum)
        cluster.stop()
        server.stop(5)
        done.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    done.wait()


if __name__ == "__main__":
    main()
