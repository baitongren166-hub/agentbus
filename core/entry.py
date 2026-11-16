"""打包入口（Windows / Mac 发行版用 PyInstaller 打成一个 agentbus-core 可执行文件）：
  agentbus-core serve [端口]   本地服务（界面 + 接口）
  agentbus-core cli ...        命令行（等于 agentbus ...）
  agentbus-core mcp            给 Claude 等用的 MCP 服务
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "serve"
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    if mode == "serve":
        import service
        service.serve(int(sys.argv[1]) if len(sys.argv) > 1 else 8790)
    elif mode == "cli":
        import agentbus
        agentbus.main()
    elif mode == "mcp":
        import mcp_server
        mcp_server.main()
    else:
        sys.exit(f"不认识：{mode}（可用 serve / cli / mcp）")


if __name__ == "__main__":
    main()
