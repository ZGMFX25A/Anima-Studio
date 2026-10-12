"""ComfyUI API-format 工作流构建器。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Out:
    """某节点的某个输出端口，可直接作为另一个节点的输入。"""
    node: str
    index: int = 0

    def ref(self) -> list:
        return [self.node, self.index]


class Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self._next = 1

    def add(self, class_type: str, title: str | None = None, **inputs: Any) -> str:
        nid = str(self._next)
        self._next += 1
        self.nodes[nid] = {
            "class_type": class_type,
            "inputs": {k: (v.ref() if isinstance(v, Out) else v) for k, v in inputs.items()},
            "_meta": {"title": title or class_type},
        }
        return nid

    def node(self, class_type: str, title: str | None = None, outputs: int = 1, **inputs: Any):
        """添加节点并返回其输出端口（outputs>1 时返回元组）。"""
        nid = self.add(class_type, title, **inputs)
        if outputs == 1:
            return Out(nid, 0)
        return tuple(Out(nid, i) for i in range(outputs))

    def to_api(self) -> dict[str, Any]:
        return self.nodes
