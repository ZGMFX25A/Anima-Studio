"""ComfyUI HTTP / WebSocket 客户端（同步，供任务线程使用）。"""
from __future__ import annotations

import json
import uuid
from typing import Any, Iterator

import requests
import websocket


class EngineUnavailable(RuntimeError):
    pass


class EngineRejected(RuntimeError):
    """ComfyUI 拒绝了工作流（节点参数校验失败等）。"""

    def __init__(self, message: str, detail: Any = None):
        super().__init__(message)
        self.detail = detail


class ComfyClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.ws_base = self.base.replace("http://", "ws://").replace("https://", "wss://")
        self.timeout = timeout
        self.s = requests.Session()

    def _get(self, path: str, **kw) -> requests.Response:
        try:
            r = self.s.get(self.base + path, timeout=self.timeout, **kw)
        except requests.ConnectionError as e:
            raise EngineUnavailable(f"ComfyUI 未响应: {self.base}") from e
        r.raise_for_status()
        return r

    def _post(self, path: str, **kw) -> requests.Response:
        try:
            return self.s.post(self.base + path, timeout=self.timeout, **kw)
        except requests.ConnectionError as e:
            raise EngineUnavailable(f"ComfyUI 未响应: {self.base}") from e

    # ── 查询 ──
    def alive(self) -> bool:
        try:
            self._get("/system_stats")
            return True
        except Exception:
            return False

    def system_stats(self) -> dict:
        return self._get("/system_stats").json()

    def object_info(self, node_class: str) -> dict:
        return self._get(f"/object_info/{node_class}").json().get(node_class, {})

    def input_choices(self, node_class: str, input_name: str) -> list[str]:
        info = self.object_info(node_class)
        for section in ("required", "optional"):
            spec = info.get("input", {}).get(section, {}).get(input_name)
            if spec:
                choices = spec[0]
                if isinstance(choices, list):
                    return list(choices)
                if choices == "COMBO" and len(spec) > 1:
                    return list(spec[1].get("options", []))
        return []

    # ── 上传 / 下载 ──
    def upload_image(self, data: bytes, filename: str, subfolder: str = "painter") -> str:
        r = self._post("/upload/image",
                       files={"image": (filename, data, "image/png")},
                       data={"type": "input", "subfolder": subfolder, "overwrite": "true"})
        r.raise_for_status()
        j = r.json()
        return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"]

    def view(self, filename: str, subfolder: str, folder_type: str) -> bytes:
        return self._get("/view", params={"filename": filename, "subfolder": subfolder, "type": folder_type}).content

    def history(self, prompt_id: str) -> dict:
        return self._get(f"/history/{prompt_id}").json().get(prompt_id, {})

    # ── 执行 ──
    def submit(self, graph: dict, client_id: str) -> str:
        r = self._post("/prompt", json={"prompt": graph, "client_id": client_id})
        if r.status_code != 200:
            try:
                body = r.json()
            except ValueError:
                body = r.text
            msg = body.get("error", {}).get("message", "workflow rejected") if isinstance(body, dict) else str(body)
            raise EngineRejected(msg, body)
        return r.json()["prompt_id"]

    def interrupt(self, prompt_id: str | None = None) -> None:
        self._post("/interrupt", json={"prompt_id": prompt_id} if prompt_id else {})

    def delete_queued(self, prompt_id: str) -> None:
        self._post("/queue", json={"delete": [prompt_id]})

    def free(self, unload_models: bool = False) -> None:
        self._post("/free", json={"unload_models": unload_models, "free_memory": True})

    def connect_ws(self, client_id: str, timeout: float = 5.0) -> websocket.WebSocket:
        try:
            return websocket.create_connection(f"{self.ws_base}/ws?clientId={client_id}", timeout=timeout)
        except (OSError, websocket.WebSocketException) as e:
            raise EngineUnavailable(f"无法连接 ComfyUI WebSocket: {e}") from e

    @staticmethod
    def events(ws: websocket.WebSocket) -> Iterator[dict | None]:
        """逐条产出 JSON 事件；超时产出 None（供调用方检查取消/超时）。二进制预览帧忽略。"""
        while True:
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                yield None
                continue
            if isinstance(msg, bytes):
                continue
            try:
                yield json.loads(msg)
            except ValueError:
                continue

    @staticmethod
    def new_client_id() -> str:
        return uuid.uuid4().hex
