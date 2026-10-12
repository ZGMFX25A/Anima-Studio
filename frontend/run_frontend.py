"""启动前端服务（嵌入式 Python 的 ._pth 不会把当前目录加入 sys.path，故由此脚本加入）。"""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
runpy.run_module("server", run_name="__main__")
