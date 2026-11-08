#!/bin/sh
# Make the stubs of head/app/ from proto/chat.proto (a copy of chat.proto of
# celaut-project/chatui master 953ea99, chat.md revision 6 round 10). Use the versions of the
# image: grpcio-tools 1.85.1 (protobuf 7.x gencode), the same as grpcio 1.85.1 and
# protobuf 7.36.2 of head/runtime.Dockerfile.
#
#   python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
#   PATH=$PWD/.venv/bin:$PATH scripts/gen_proto.sh
set -eu
cd "$(dirname "$0")/.."
python3 -m grpc_tools.protoc -I proto --python_out=head/app --grpc_python_out=head/app proto/chat.proto
# chat.proto has no package, so the stub imports its module by a bare name.
sed -i.bak 's/^import chat_pb2 as chat__pb2$/from . import chat_pb2 as chat__pb2/' head/app/chat_pb2_grpc.py
rm -f head/app/chat_pb2_grpc.py.bak
