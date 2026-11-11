"""Build helper: gzip one file reproducibly (no timestamp). Usage: gzip_file.py SRC DST"""
import gzip
import sys

with open(sys.argv[1], "rb") as src:
    data = src.read()
with open(sys.argv[2], "wb") as dst:
    with gzip.GzipFile(filename="", mode="wb", fileobj=dst, compresslevel=9, mtime=0) as gz:
        gz.write(data)
