"""图形化菜单入口，由项目根目录的「启动菜单.bat」调用。

菜单文字放在这里而不是直接写进 bat，是因为 cmd.exe 解析 UTF-8 的批处理文件会出错
（多字节汉字会被拆断，后半截被当成命令执行），而改用 GBK 保存的话，
在 PyCharm 等默认 UTF-8 的编辑器里又会显示成乱码。
把 bat 保持成纯 ASCII、中文全部交给 Python 输出，两边就都没问题了。
"""

import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (显示名, 相对项目根目录的脚本路径, 运行前的提示语)
ACTIONS = {
    "1": ("检查荼蘼面板标定", "manage/calibrate.py", None),
    "2": ("添加或修改角色", "manage/roles.py", None),
    "3": ("开始挂机", "daily_cleanup.py",
          "开始挂机前请确认：\n"
          "  - 已经手动关掉所有「一梦江湖」窗口\n"
          "  - 接下来不要操作鼠标键盘，也不要让屏幕息屏或锁屏"),
    "4": ("查看运行状态", "manage/server.py",
          "正在启动查看页面，浏览器会自动打开。\n"
          "看完后回到这个窗口按 Ctrl+C 关闭"),
    # 重跑的开跑提示由 rerun.py 在选完角色之后自己打印，这里不重复提示
    "5": ("重跑指定角色", "manage/rerun.py", None),
}


def clear():
    os.system("cls")


def print_menu():
    print("=" * 60)
    print("                   clx_auto 挂机助手")
    print("=" * 60)
    print()
    print("  第一次使用请按 1 - 2 的顺序做完准备工作")
    print()
    print("  1. 检查荼蘼面板标定（第一次使用/换了电脑或分辨率时）")
    print("  2. 添加或修改角色")
    print()
    print("  3. 开始挂机")
    print("  4. 查看运行状态")
    print("  5. 重跑指定角色（只重跑失败的那几个，需等当前这轮跑完）")
    print()
    print("  0. 退出")
    print()
    print("=" * 60)


def run_action(choice):
    label, script, notice = ACTIONS[choice]
    clear()
    if notice:
        print(notice)
        print()
        input("准备好后按回车继续...")
        clear()

    script_path = os.path.join(BASE_DIR, script.replace("/", os.sep))
    if not os.path.isfile(script_path):
        print(f"找不到 {script}，项目文件可能不完整")
    else:
        # 用当前解释器跑子脚本，保证和菜单用的是同一个Python环境；
        # cwd固定到项目根目录，因为daily_cleanup.py用相对路径读config.yaml、写state/和screenshots/
        try:
            subprocess.run([sys.executable, script_path], cwd=BASE_DIR)
        except KeyboardInterrupt:
            print("\n已中断")
    print()
    input(f"【{label}】已结束，按回车返回菜单...")


def main():
    while True:
        clear()
        print_menu()
        try:
            choice = input("请输入编号后按回车: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if choice == "0":
            return
        if choice in ACTIONS:
            run_action(choice)
        else:
            input("无效的选项，按回车重新选择...")


if __name__ == "__main__":
    main()
