import pyperclip
import re
import time
import threading
from colorama import Fore, Style, init

init()

def main():
    while True:
        title = input(Fore.CYAN + "Enter a title: " + Style.RESET_ALL)
        # Remove extra whitespaces
        title = re.sub(r'\s+', ' ', title).strip()
        # Format as filename: lowercase, replace spaces with _, remove invalid chars
        formatted_title = re.sub(r'[^\w\s-]', '', title)  # remove special chars except - and _
        formatted_title = re.sub(r'\s+', '_', formatted_title).lower()
        pyperclip.copy(formatted_title)
        print(Fore.GREEN + "Formatted Title:", formatted_title + Style.RESET_ALL)
        print()
        # Erase the lines after 7 seconds
        def erase():
            time.sleep(7)
            print("\033[s\033[3F\033[K\033[B\033[K\033[B\033[K\033[u", end="", flush=True)
        threading.Thread(target=erase).start()

if __name__ == "__main__":
    main()