# 模块入口：支持 python -m email_agent 运行
from email_agent.cli.main import main

if __name__ == "__main__":
    # 直接调用 CLI 主函数，参数从 sys.argv 自动解析
    main()
