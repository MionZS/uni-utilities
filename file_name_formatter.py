import re
import threading
import time

from colorama import Fore, Style, init
from caseconverter import snakecase
import pyperclip

init()

def main():
    while True:
        title = input(Fore.CYAN + "Enter a title: " + Style.RESET_ALL)
        # Remove extra whitespaces
        title = re.sub(r'\s+', ' ', title).strip()
        # Format as filename: remove invalid chars, then convert to snake_case
        cleaned_title = re.sub(r'[^\w\s-]', '', title)
        formatted_title = snakecase(cleaned_title)
        pyperclip.copy(formatted_title)
        print(Fore.GREEN + "Formatted Title:", formatted_title + Style.RESET_ALL)
        print()
        # Erase the lines after 7 seconds
        def erase():
            time.sleep(7)
            print("\033[s\033[3F\033[K\033[B\033[K\033[B\033[K\033[u\033[2K\033[3A\033[G" + Fore.CYAN + "Enter a title: " + Style.RESET_ALL, end="", flush=True)
        threading.Thread(target=erase).start()

if __name__ == "__main__":
    main()