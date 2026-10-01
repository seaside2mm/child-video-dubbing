"""User-run, hidden token entry. Never receives a secret as a CLI argument."""
from getpass import getpass


def main():
    from huggingface_hub import login

    print("请先在 Hugging Face 登录并接受 Community-1 的模型访问条件。")
    print("在下方粘贴只读访问令牌；输入不会显示，也不要发到聊天。")
    token = getpass("Hugging Face token: ").strip()
    if not token:
        print("未输入令牌，未保存任何配置。")
        return 1
    try:
        login(token=token, add_to_git_credential=False)
    except Exception as exc:
        # Do not echo request bodies, authentication headers, or the token.
        print(f"登录验证未成功（{type(exc).__name__}），请检查网络和令牌权限。")
        return 1
    finally:
        token = ""
    print("令牌已由 Hugging Face 官方库保存在本机用户缓存；未配置 Git 凭据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
