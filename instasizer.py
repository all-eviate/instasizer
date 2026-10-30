"""INSTASIZER 실행 진입점.

이전 Tkinter 버전(단계별 대화상자 방식)은 instasizer_tk.py에 남아 있습니다.
"""
import sys

from sizer.gui import main

if __name__ == "__main__":
    sys.exit(main())
