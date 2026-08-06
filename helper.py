import pyautogui
import tkinter as tk

def update_position_label():
    # 获取鼠标当前位置的坐标
    x, y = pyautogui.position()
    position_label.configure(text=f"坐标：({x}, {y})")
    position_label.after(100, update_position_label)

# 创建主窗口
window = tk.Tk()
window.title("鼠标坐标")
window.geometry("200x50")

# 创建坐标标签
position_label = tk.Label(window, text="坐标：(0, 0)")
position_label.pack()

# 更新坐标标签
update_position_label()

# 设置窗口始终在最顶层显示
window.attributes("-topmost", True)

# 隐藏窗口标题栏
window.overrideredirect(True)

# 窗口跟随鼠标移动
def move_window(event):
    x, y = event.x_root, event.y_root
    window.geometry(f"+{x}+{y}")

window.bind("<Motion>", move_window)

# 运行窗口主循环
window.mainloop()