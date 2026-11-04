#!/bin/sh
# Build the macOS SCSI pass-through helper. No dependencies beyond the
# Xcode command line tools (clang + the macOS SDK headers).
set -eu
cd "$(dirname "$0")"
mkdir -p build
clang -O2 -Wall -Wextra -std=c11 -o build/discprobe-spt discprobe-spt.c \
      -framework IOKit -framework CoreFoundation -framework DiskArbitration
echo "built: $(pwd)/build/discprobe-spt"
