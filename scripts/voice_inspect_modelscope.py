"""Inspect the official ModelScope ZIP using HTTP ranges before downloading it."""
import io
import json
import urllib.request
import zipfile

URL = "https://www.modelscope.cn/models/XXXXRT/GPT-SoVITS-Pretrained/resolve/master/pretrained_models.zip"


class RemoteZip(io.RawIOBase):
    def __init__(self, url):
        self.url = url
        req = urllib.request.Request(url, headers={"Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=30) as response:
            self.size = int(response.headers["Content-Range"].split("/")[-1])
            response.read(1)
        self.position = 0

    def seekable(self):
        return True

    def readable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=0):
        self.position = offset if whence == 0 else (self.position if whence == 1 else self.size) + offset
        return self.position

    def read(self, size=-1):
        if self.position >= self.size:
            return b""
        end = self.size - 1 if size < 0 else min(self.size - 1, self.position + size - 1)
        request = urllib.request.Request(self.url, headers={"Range": f"bytes={self.position}-{end}"})
        with urllib.request.urlopen(request, timeout=90) as response:
            content_range = response.headers.get("Content-Range", "")
            if not content_range.startswith(f"bytes {self.position}-"):
                raise IOError(f"Server did not honor range: {content_range}")
            data = response.read()
        self.position += len(data)
        return data


if __name__ == "__main__":
    remote = RemoteZip(URL)
    print("Archive bytes:", remote.size, flush=True)
    with zipfile.ZipFile(remote) as archive:
        for info in archive.infolist():
            print(json.dumps({"name": info.filename, "bytes": info.file_size, "compressed": info.compress_size}), flush=True)
