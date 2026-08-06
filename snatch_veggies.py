import datetime
import logging
import sys

import pyautogui
import time

logger = logging.getLogger(__name__)
today = datetime.datetime.now()
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

if __name__ == '__main__':
    logger.info("即将开始扫神厨菜...")
    time.sleep(5)
    while True:
        time.sleep(3)
        pyautogui.click(3136, 469)
        try:
            account_field_pos = pyautogui.locateCenterOnScreen("veggies/he_huan_list.png", confidence=0.8)
            logger.info("已找到菜品，点进店铺")
            pyautogui.click(account_field_pos)
            try:
                flag = 4
                while flag > 0:
                    account_field_pos = pyautogui.locateCenterOnScreen("veggies/buy.png", confidence=0.8)
                    logger.info("已找到菜品，购买...")
                    pyautogui.click(account_field_pos)
                    pyautogui.click(2500, 1482)
                    flag -= 1
            except pyautogui.ImageNotFoundException:
                logger.info("点进来没有菜品，继续寻找...")
        except pyautogui.ImageNotFoundException:
            logger.info("未找到菜品，继续寻找...")