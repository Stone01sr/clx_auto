"""角色管理助手：新增角色、修改已有角色。

新增：交互式地引导完成一个新角色的全部准备工作——框选并保存账号图片、登录页角色图片、
要运行的脚本图片（可复用已有角色的），最后把角色配置追加写回 config.yaml。
修改：重新截取某张图片、更换脚本、切换渠道服标记或启用状态，只替换该角色那一块配置。

用法：
    python manage/roles.py

截图分两步，是为了绕开"下拉列表一失去焦点就收起"的问题：
先把目标界面（含展开的下拉列表）调好并保持在最前，按全局热键 F9 定格整屏；
再在定格下来的静态图上拖拽框选目标区域。截图那一刻本程序不会抢焦点，
框选阶段面对的又是一张静态图，所以列表不会被收起。
"""

import datetime
import os
import re
import shutil
import sys
import time
import tkinter as tk

import pyautogui
import win32api
import win32con
import yaml
from PIL import ImageTk

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
RES_DIR = os.path.join(BASE_DIR, "res")

# 触发定格截屏的全局热键。荼蘼的方案下拉列表、游戏的角色下拉列表都是一失去焦点就收起，
# 所以不能让用户切回终端按回车触发，必须用不抢焦点的全局热键
CAPTURE_HOTKEY_VK = win32con.VK_F9
CAPTURE_HOTKEY_NAME = "F9"

# 角色名只允许蛇形命名，和config.yaml里已有的角色保持一致，同时保证能直接拼进图片文件名
ROLE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def wait_for_capture_hotkey():
    """轮询全局键盘状态等待热键，按下热键返回True，按Esc取消返回False。

    用 GetAsyncKeyState 查询全局按键状态，本程序全程不需要获得焦点，
    用户可以让展开着下拉列表的目标窗口一直停在最前面。
    """
    # 先读一次把两个键的历史状态清掉，避免把进入本函数之前的按键当成这次的触发
    win32api.GetAsyncKeyState(CAPTURE_HOTKEY_VK)
    win32api.GetAsyncKeyState(win32con.VK_ESCAPE)
    while True:
        if win32api.GetAsyncKeyState(CAPTURE_HOTKEY_VK) & 0x8000:
            return True
        if win32api.GetAsyncKeyState(win32con.VK_ESCAPE) & 0x8000:
            return False
        time.sleep(0.05)


class ImageRegionSelector:
    """在一张已经定格的整屏截图上拖拽框选，返回裁剪区域。

    因为框选是在静态图上做的，这个窗口抢不抢焦点都无所谓了——
    目标界面上展开的下拉列表在截图那一刻就已经被记录进图里。
    """

    def __init__(self, image):
        self.image = image
        self.box = None          # 最终裁剪框，图片像素坐标 (left, top, right, bottom)
        self.cancelled = False
        self.start = None
        self.root = None
        self.canvas = None
        self.rect_id = None
        self.scale = 1.0

    def select(self, hint):
        """显示定格图等待框选，返回(left, top, right, bottom)；取消或区域过小返回None"""
        self.root = tk.Tk()
        self.root.attributes("-topmost", True)
        self.root.title("框选目标区域")

        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        # 正常情况下截图尺寸和屏幕一致、scale为1；DPI缩放导致两者不一致时按比例缩放显示，
        # 框选坐标再按同样比例换算回原图像素，保证裁出来的就是用户框中的那块
        self.scale = min(screen_w / self.image.width, screen_h / self.image.height, 1.0)
        disp_w = int(self.image.width * self.scale)
        disp_h = int(self.image.height * self.scale)

        self.root.geometry(f"{disp_w}x{disp_h}+0+0")
        self.root.overrideredirect(True)

        display_image = self.image if self.scale == 1.0 else self.image.resize((disp_w, disp_h))
        self.photo = ImageTk.PhotoImage(display_image)

        self.canvas = tk.Canvas(self.root, width=disp_w, height=disp_h,
                                highlightthickness=0, cursor="cross")
        self.canvas.pack()
        self.canvas.create_image(0, 0, image=self.photo, anchor=tk.NW)
        self.canvas.create_rectangle(0, 0, disp_w, 96, fill="black", stipple="gray50", outline="")
        self.canvas.create_text(
            disp_w // 2, 48,
            text=f"{hint}\n（画面已定格）拖拽鼠标框选目标区域，松开完成；按 Esc 或点右键取消",
            fill="#ffe97f", font=("Microsoft YaHei", 18, "bold"), justify=tk.CENTER,
        )

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        # 这是个无边框全屏窗口，没有关闭按钮，取消的路子必须留够：
        # Esc在root和canvas上都绑一次（无边框窗口拿键盘焦点有时不稳），再加一个右键兜底
        self.root.bind("<Escape>", self._on_escape)
        self.canvas.bind("<Escape>", self._on_escape)
        self.canvas.bind("<Button-3>", self._on_escape)
        self.root.focus_force()
        self.canvas.focus_set()
        self.root.mainloop()

        if self.cancelled or not self.box:
            return None
        left, top, right, bottom = self.box
        if right - left < 5 or bottom - top < 5:
            print("  框选区域太小（可能只是点了一下），已忽略")
            return None
        return self.box

    def _on_press(self, event):
        self.start = (event.x, event.y)
        self.rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="red", width=2)

    def _on_drag(self, event):
        if self.rect_id:
            self.canvas.coords(self.rect_id, self.start[0], self.start[1], event.x, event.y)

    def _on_release(self, event):
        if not self.start:
            return
        left, right = sorted((self.start[0], event.x))
        top, bottom = sorted((self.start[1], event.y))
        # 画布坐标换算回原始截图的像素坐标
        self.box = (int(left / self.scale), int(top / self.scale),
                    int(right / self.scale), int(bottom / self.scale))
        self._close()

    def _on_escape(self, event):
        self.cancelled = True
        self._close()

    def _close(self):
        self.root.quit()
        self.root.destroy()

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
RES_DIR = os.path.join(BASE_DIR, "res")

# 角色名只允许蛇形命名，和config.yaml里已有的角色保持一致，同时保证能直接拼进图片文件名
ROLE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def capture_region_to_file(hint, save_path):
    """引导用户定格截屏并框选一块区域保存为png，成功返回True。

    两步走：先等全局热键定格整屏（此时本程序不抢焦点，目标界面展开的下拉列表不会收起），
    再在定格图上框选并直接裁剪——不需要二次截屏，所见即所得。
    """
    print(f"   {hint}")
    print(f"   调好界面后按 {CAPTURE_HOTKEY_NAME} 定格整屏（按 Esc 取消本次截取）...")
    if not wait_for_capture_hotkey():
        return False

    screen = pyautogui.screenshot()
    print(f"   已定格整屏（{screen.width}x{screen.height}），请在弹出的画面上框选目标区域")
    box = ImageRegionSelector(screen).select(hint)
    if not box:
        return False

    image = screen.crop(box)
    image.save(save_path)
    print(f"  已保存: {os.path.relpath(save_path, BASE_DIR)}  (尺寸 {image.width}x{image.height})")
    return True


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ask(prompt, default=None):
    """读一行输入，直接回车则用默认值"""
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default


def ask_yes_no(prompt, default=False):
    hint = "y/N" if not default else "Y/n"
    value = input(f"{prompt} [{hint}]: ").strip().lower()
    if not value:
        return default
    return value in ("y", "yes")


def ask_role_name(existing_names):
    while True:
        name = ask("角色名（蛇形命名，会用作图片文件名前缀，如 an_an）")
        if not ROLE_NAME_PATTERN.match(name):
            print("  角色名只能是小写字母、数字和下划线，且以字母开头，请重新输入")
            continue
        if name in existing_names:
            print(f"  角色 {name} 已存在于 config.yaml，请换一个名字")
            continue
        return name


def capture_with_retry(hint, save_path, what):
    """定格截屏+框选保存一张图，保存后让用户确认，不满意可以重拍；确认放弃则返回False。

    这里刻意不在截图前用input()等回车：回终端按回车会让目标窗口失去焦点，
    荼蘼的方案下拉列表、游戏的角色下拉列表都会因此收起，导致截不到想要的内容。
    截完之后再回终端确认就没有这个问题了，那时画面已经定格保存下来了。

    图片是一张张即时落盘的，而配置要走完全部步骤才写回config.yaml，所以中途退出再重来时，
    之前截好的图还在。这里先问一句是否直接复用，避免每次都得从头重截。
    """
    if os.path.isfile(save_path):
        captured_at = datetime.datetime.fromtimestamp(os.path.getmtime(save_path))
        print(f"\n>> 【{what}】已存在: {os.path.relpath(save_path, BASE_DIR)}"
              f"（截于 {captured_at:%Y-%m-%d %H:%M:%S}）")
        if ask_yes_no("   直接复用这张，跳过重新截取？", default=True):
            return True

    while True:
        print(f"\n>> 准备截取【{what}】")
        if not capture_region_to_file(hint, save_path):
            if ask_yes_no("  本次截取已取消，要重新截取吗？", default=True):
                continue
            return False
        if ask_yes_no(f"  确认这张【{what}】可用吗？（选n重新截取）", default=True):
            return True


def list_existing_script_images(config):
    """列出可复用的脚本图片，按config.yaml里角色的配置顺序排列（很多角色跑的是同一个方案，
    同一张图会被多个角色引用，这里去重但保留首次出现的顺序）；末尾补上res/里存在、
    但当前没有任何角色引用的脚本图片。返回 [(图片相对路径, [引用它的角色名, ...]), ...]"""
    ordered = []
    users = {}
    for role in config.get("roles", []):
        path = role.get("script_image")
        if not path:
            continue
        if path not in users:
            users[path] = []
            ordered.append(path)
        users[path].append(role["name"])

    if os.path.isdir(RES_DIR):
        for name in sorted(os.listdir(RES_DIR)):
            if not name.endswith("_script_item.png"):
                continue
            path = f"res/{name}"
            if path not in users:
                users[path] = []
                ordered.append(path)

    return [(path, users[path]) for path in ordered]


def choose_script_image(role_name, config):
    """返回要写进config.yaml的脚本图片相对路径；取消返回None"""
    existing = list_existing_script_images(config)
    print("\n>> 该角色要运行的脚本图片：")
    if existing:
        print("   已有的脚本图片（可直接复用，按config.yaml中的角色顺序排列）：")
        width = max(len(path) for path, _ in existing)
        for i, (path, role_users) in enumerate(existing, 1):
            used_by = "、".join(role_users) if role_users else "当前无角色使用"
            print(f"     {i:>2}. {path.ljust(width)}   ({used_by})")
        print("      0. 重新截取一张新的")
        while True:
            choice = ask("   请选择编号", default="0")
            if not choice.isdigit():
                print("     请输入数字编号")
                continue
            index = int(choice)
            if index == 0:
                break
            if 1 <= index <= len(existing):
                return existing[index - 1][0]
            print("     编号超出范围，请重新选择")

    save_path = os.path.join(RES_DIR, f"{role_name}_script_item.png")
    if capture_with_retry("请框选荼蘼方案下拉列表中该角色要运行的脚本条目",
                          save_path, "脚本图片"):
        return f"res/{role_name}_script_item.png"
    return None


def _with_comment(text, comment, width=52):
    """把行尾注释对齐到第width列；角色名较长导致本身就超过width时，至少留两个空格，
    否则注释会直接粘在值后面（`"xxx.png"# 注释`），排版难看"""
    return text + " " * max(width - len(text), 2) + comment


def build_role_entry(role_name, account_image, login_role_image, script_image,
                     channel_account, enable=True):
    """按config.yaml里已有角色的书写风格拼一段yaml文本。
    不用yaml.dump整体重写配置，是为了保留文件里原有的注释和排版。"""
    lines = [f'  - name: "{role_name}"']
    if account_image:
        lines.append(_with_comment(f'    account_image: "{account_image}"', "# 账号图片"))
    lines.append(_with_comment(f'    login_role_image: "{login_role_image}"', "# 登录页面角色选择图片"))
    lines.append(_with_comment(f'    script_image: "{script_image}"', "# 角色要挂的脚本图片"))
    if channel_account:
        lines.append(_with_comment("    channel_account: true",
                                    "# 渠道服账号，需借助idv-login辅助登录，不能直接登录"))
    lines.append(f"    enable: {'true' if enable else 'false'}")
    return "\n".join(lines) + "\n"


def _read_config_lines():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return f.readlines()


def _write_config_lines(lines):
    """备份原配置后原子写回。配置文件是整个项目的唯一配置来源，改坏了代价高，
    所以先copy一份带时间戳的备份，再写临时文件+os.replace替换，避免写到一半崩掉留下半个文件。"""
    backup_path = f"{CONFIG_PATH}.{datetime.datetime.now():%Y%m%d_%H%M%S}.bak"
    shutil.copy2(CONFIG_PATH, backup_path)

    tmp_path = CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp_path, CONFIG_PATH)
    return backup_path


def _find_roles_section_end(lines, start):
    """返回roles列表最后一行的下一个位置（有缩进的非空行都还属于这个列表）"""
    end = start + 1
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        if not line[0].isspace():  # 顶层的注释或配置项，roles列表到此为止
            break
        end = i + 1
    return end


def find_role_block(lines, role_name):
    """定位某个角色在config.yaml中的行区间[start, end)。
    从 `- name: "角色名"` 那一行开始，一直到下一个列表项（同级的 `- `）或列表结束为止。"""
    start = None
    pattern = re.compile(r'^\s*-\s+name:\s*["\']?' + re.escape(role_name) + r'["\']?\s*(#.*)?$')
    for i, line in enumerate(lines):
        if pattern.match(line):
            start = i
            break
    if start is None:
        return None

    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            continue
        # 顶层内容，或同级的下一个列表项，都表示当前角色的配置块结束了
        if not line[0].isspace() or re.match(r"^\s{0,2}-\s+", line):
            end = i
            break
    else:
        end = len(lines)

    # 把块尾部的空行留给下一个块，保持原有的"角色之间空一行"排版
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1
    return start, end


def append_role_to_config(entry_text):
    """把新角色插到roles列表末尾，使其落在最后一个角色之后、下一个顶层配置段
    （及其上方注释）之前，不会串到别的段里去。"""
    lines = _read_config_lines()
    start = next((i for i, line in enumerate(lines) if re.match(r"^roles:\s*$", line)), None)
    if start is None:
        raise RuntimeError("config.yaml 中找不到顶层的 roles: 配置项，无法自动插入")

    lines.insert(_find_roles_section_end(lines, start), "\n" + entry_text)
    return _write_config_lines(lines)


def replace_role_in_config(role_name, entry_text):
    """用新内容整体替换某个角色的配置块。只动这一块，文件里其他角色和注释都原样保留；
    但该角色块内原有的自定义注释会被这里生成的标准注释覆盖。"""
    lines = _read_config_lines()
    block = find_role_block(lines, role_name)
    if not block:
        raise RuntimeError(f"config.yaml 中找不到角色 {role_name} 的配置块，无法修改")
    start, end = block
    lines[start:end] = [entry_text]
    return _write_config_lines(lines)


def capture_account_image(role_name):
    """截取账号图片，成功返回配置里要写的相对路径，取消返回None"""
    save_path = os.path.join(RES_DIR, f"{role_name}_account.png")
    if capture_with_retry("请框选登录界面账号下拉列表中该角色的账号条目", save_path, "账号图片"):
        return f"res/{role_name}_account.png"
    return None


def capture_login_role_image(role_name):
    """截取登录页角色图片，成功返回配置里要写的相对路径，取消返回None"""
    save_path = os.path.join(RES_DIR, f"{role_name}_role_login.png")
    if capture_with_retry("请框选登录界面角色选择列表中该角色的条目", save_path, "角色图片"):
        return f"res/{role_name}_role_login.png"
    return None


def print_role_list(config):
    """按config.yaml的配置顺序打印角色一览（这个顺序同时也是队列的入队顺序，不要排序打乱）"""
    roles = config.get("roles", [])
    if not roles:
        print("当前 config.yaml 中还没有配置任何角色\n")
        return roles
    width = max(len(r["name"]) for r in roles)
    print(f"当前 config.yaml 中已有 {len(roles)} 个角色（按配置顺序，也是队列入队顺序）:")
    for i, role in enumerate(roles, 1):
        flag = "渠道服" if role.get("channel_account") else "官服 "
        state = "启用" if role.get("enable", True) else "停用"
        print(f"  {i:>2}. {role['name'].ljust(width)}   [{state}]  {flag}")
    print()
    return roles


def add_role_flow(config):
    """新增一个角色：截图 -> 选脚本 -> 追加写入config.yaml"""
    existing_names = [r["name"] for r in config.get("roles", [])]
    role_name = ask_role_name(existing_names)
    channel_account = ask_yes_no("是否是渠道服账号（需要idv-login辅助登录）？", default=False)

    os.makedirs(RES_DIR, exist_ok=True)

    # 渠道服账号走idv-login登录，不经过账号下拉框，因此不需要账号图片
    account_image = None
    if channel_account:
        print("\n渠道服账号通过 idv-login 登录，不需要账号图片，跳过该步骤")
    else:
        account_image = capture_account_image(role_name)
        if not account_image:
            print("\n已取消（账号图片是官服角色登录的必需材料），未修改任何配置")
            return

    login_role_image = capture_login_role_image(role_name)
    if not login_role_image:
        print("\n已取消，未修改任何配置")
        return

    script_image = choose_script_image(role_name, config)
    if not script_image:
        print("\n已取消，未修改任何配置")
        return

    entry_text = build_role_entry(role_name, account_image, login_role_image,
                                  script_image, channel_account)
    print("\n" + "=" * 60)
    print("即将追加到 config.yaml 的 roles 列表：\n")
    print(entry_text)
    print("=" * 60)
    if not ask_yes_no("确认写入 config.yaml 吗？", default=True):
        print("已放弃写入，截好的图片仍保留在 res/ 下，可稍后手动配置")
        return

    backup_path = append_role_to_config(entry_text)
    print(f"\n完成！角色 {role_name} 已加入 config.yaml（原文件已备份为 "
          f"{os.path.basename(backup_path)}）")
    print("下次运行 daily_cleanup.py 时该角色就会自动进入队列。")


def pick_role(config, prompt="请选择要修改的角色编号"):
    """让用户按编号挑一个已有角色，返回该角色的配置dict；选0返回None"""
    roles = config.get("roles", [])
    if not roles:
        return None
    while True:
        choice = ask(f"{prompt}（0 返回上级菜单）", default="0")
        if not choice.isdigit():
            print("  请输入数字编号")
            continue
        index = int(choice)
        if index == 0:
            return None
        if 1 <= index <= len(roles):
            return roles[index - 1]
        print("  编号超出范围，请重新选择")


def edit_role_flow(config):
    """修改一个已有角色：可重截图片、换脚本、切换渠道服标记和启用状态"""
    roles = print_role_list(config)
    if not roles:
        return
    role = pick_role(config)
    if not role:
        return

    role_name = role["name"]
    # 全部改动先攒在这几个变量里，最后确认时才一次性写回配置，中途退出不会留下半套改动
    account_image = role.get("account_image")
    login_role_image = role.get("login_role_image")
    script_image = role.get("script_image")
    channel_account = bool(role.get("channel_account", False))
    enable = bool(role.get("enable", True))
    dirty = False

    while True:
        print("\n" + "-" * 60)
        print(f"正在修改角色: {role_name}{'   (有未保存的改动)' if dirty else ''}")
        print(f"    账号图片:  {account_image or '（渠道服账号，不需要）'}")
        print(f"    角色图片:  {login_role_image}")
        print(f"    脚本图片:  {script_image}")
        print(f"    渠道服:    {'是' if channel_account else '否'}")
        print(f"    启用:      {'是' if enable else '否'}")
        print("-" * 60)
        print("  1. 重新截取账号图片" + ("（渠道服账号不需要）" if channel_account else ""))
        print("  2. 重新截取角色图片")
        print("  3. 更换脚本图片")
        print(f"  4. 切换渠道服标记（当前: {'是' if channel_account else '否'}）")
        print(f"  5. 切换启用状态（当前: {'启用' if enable else '停用'}）")
        print("  0. 保存并返回")
        print("  q. 放弃改动返回")

        choice = ask("请选择", default="0").lower()

        if choice == "1":
            if channel_account:
                print("  渠道服账号通过 idv-login 登录，不使用账号图片，无需截取")
                continue
            # 重截会覆盖同名旧文件，所以先临时挪开，让capture_with_retry不走"复用已有"分支
            path = os.path.join(RES_DIR, f"{role_name}_account.png")
            new_image = _recapture(path, capture_account_image, role_name)
            if new_image:
                account_image, dirty = new_image, True
        elif choice == "2":
            path = os.path.join(RES_DIR, f"{role_name}_role_login.png")
            new_image = _recapture(path, capture_login_role_image, role_name)
            if new_image:
                login_role_image, dirty = new_image, True
        elif choice == "3":
            new_image = choose_script_image(role_name, config)
            if new_image:
                script_image, dirty = new_image, True
        elif choice == "4":
            channel_account = not channel_account
            dirty = True
            if channel_account:
                print("  已标记为渠道服账号；账号图片对渠道服无效，保存时会从配置中移除")
                account_image = None
            else:
                print("  已改回官服账号，需要账号图片才能登录")
                if not account_image:
                    if ask_yes_no("  现在截取账号图片吗？", default=True):
                        account_image = capture_account_image(role_name)
        elif choice == "5":
            enable = not enable
            dirty = True
            print(f"  已切换为{'启用' if enable else '停用'}")
        elif choice == "0":
            break
        elif choice == "q":
            if not dirty or ask_yes_no("  确认放弃本次所有改动吗？", default=False):
                print("  已放弃改动，config.yaml 未被修改")
                return
        else:
            print("  无效的选项")

    if not channel_account and not account_image:
        print("\n官服角色必须有账号图片，无法保存。config.yaml 未被修改")
        return
    if not dirty:
        print("\n没有任何改动，config.yaml 未被修改")
        return

    entry_text = build_role_entry(role_name, account_image, login_role_image,
                                  script_image, channel_account, enable)
    print("\n" + "=" * 60)
    print(f"即将用下面的内容替换 config.yaml 中 {role_name} 的配置：\n")
    print(entry_text)
    print("=" * 60)
    if not ask_yes_no("确认写入 config.yaml 吗？", default=True):
        print("已放弃写入，config.yaml 未被修改")
        return

    backup_path = replace_role_in_config(role_name, entry_text)
    print(f"\n完成！角色 {role_name} 的配置已更新（原文件已备份为 "
          f"{os.path.basename(backup_path)}）")


def _recapture(existing_path, capture_fn, role_name):
    """重新截取会覆盖同名旧文件。capture_with_retry遇到已存在的文件会先问"是否复用"，
    但这里用户明确就是要重截，所以先把旧文件临时改名避开那个分支；
    截取成功就删掉旧文件，取消或失败则把旧文件恢复回来。"""
    backup = None
    if os.path.isfile(existing_path):
        backup = existing_path + ".prev"
        os.replace(existing_path, backup)
    try:
        result = capture_fn(role_name)
    except Exception:
        if backup:
            os.replace(backup, existing_path)
        raise
    if result:
        if backup and os.path.isfile(backup):
            os.remove(backup)
        return result
    if backup:
        os.replace(backup, existing_path)
        print("  已取消重新截取，保留原有图片")
    return None


def main():
    print("=" * 60)
    print("角色管理助手")
    print("=" * 60)

    while True:
        config = load_config()   # 每轮重新读，保证看到的是上一步刚写回的最新内容
        print()
        print_role_list(config)
        print("请选择操作:")
        print("  1. 新增角色")
        print("  2. 修改已有角色")
        print("  0. 退出")
        choice = ask("请输入编号", default="0")

        if choice == "1":
            add_role_flow(config)
        elif choice == "2":
            edit_role_flow(config)
        elif choice == "0":
            print("已退出")
            return
        else:
            print("无效的选项，请重新选择")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断，未修改任何配置")
