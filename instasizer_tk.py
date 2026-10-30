import os
from pathlib import Path
from tkinter import Tk, filedialog, messagebox, Toplevel, Label, Button, Radiobutton, StringVar, colorchooser, Frame
from tkinter.ttk import Progressbar
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
import pillow_heif

pillow_heif.register_heif_opener()

class ImageConverterApp:
    def __init__(self, root):
        self.root = root
        self.root.title("정방형 이미지 변환기")
        self.root.geometry("350x350")
        self.root.attributes('-topmost', True)

        # 상태 변수
        self.bg_mode = StringVar(value="black")
        self.bg_image_path = None
        self.custom_rgb = None

        main_frame = Frame(root)
        main_frame.pack(pady=20)

        # UI 구성
        Label(root, text="배경 모드를 선택하세요", font=("Arial", 11, "bold"), pady=10).pack()

        modes = [
            ("검정색 (Black)", "black"),
            ("흰색 (White)", "white"),
            ("흐림 (Blur)", "blur"),
            ("사용자 색상", "custom"),
            ("사용자 이미지", "image")
        ]

        for i, (text, mode) in enumerate(modes):
            # 1. 라디오 버튼 생성 (0열)
            rb = Radiobutton(main_frame, text=text, variable=self.bg_mode, value=mode,
                             command=self.toggle_bg_button)
            rb.grid(row=i, column=0, sticky="w", padx=10, pady=5)

            # 2. 특정 모드일 때 옆에 버튼 배치 (1열)
            if mode == "custom":
                self.color_btn = Button(main_frame, text="색상 선택", command=self.pick_color, state="disabled")
                self.color_btn.grid(row=i, column=1, padx=5)
                self.default_bg = self.color_btn.cget("bg")

            elif mode == "image":
                self.bg_btn = Button(main_frame, text="배경 파일 선택", command=self.select_bg_image, state="disabled")
                self.bg_btn.grid(row=i, column=1, padx=5)

        # 시작 버튼은 프레임 바깥 하단에 배치
        Button(root, text="변환할 이미지 선택 및 시작", command=self.start_processing,
               bg="#4CAF50", fg="white", font=("Arial", 10, "bold"), pady=10).pack(fill="x", padx=40, pady=20)

    def disable_bg_btn(self):
        self.bg_btn.config(state="disabled")
        self.bg_image_path = None

    def disable_color_btn(self):
        self.color_btn.config(state="disabled", bg=self.default_bg)
        self.custom_rgb = None

    def toggle_bg_button(self):
        """image 모드일 때만 배경 선택 버튼 활성화"""
        if self.bg_mode.get() == "image":
            self.bg_btn.config(state="normal")
            self.disable_color_btn()
        elif self.bg_mode.get() == "custom":
            self.disable_bg_btn()
            self.color_btn.config(state="normal")
        else:
            self.disable_bg_btn()
            self.disable_color_btn()

    def pick_color(self):
        color_code = colorchooser.askcolor(title="배경 색상을 선택하세요")
        if color_code[0]:
            self.custom_rgb = tuple(map(int, color_code[0]))
            self.color_btn.config(bg=color_code[1])

    def select_bg_image(self):
        path = filedialog.askopenfilename(title="배경으로 쓸 이미지 선택",
                                          filetypes=[("Image files", "*.jpg *.jpeg *.png *.bmp")])
        if path:
            self.bg_image_path = path
            messagebox.showinfo("확인", f"배경 이미지가 설정되었습니다.\n{Path(path).name}")

    def start_processing(self):
        # image 모드인데 배경을 안 골랐을 경우 체크
        if self.bg_mode.get() == "custom":
            messagebox.showwarning("주의", "색상을 먼저 선택해주세요.")
            return

        if self.bg_mode.get() == "image" and not self.bg_image_path:
            messagebox.showwarning("주의", "배경 이미지를 먼저 선택해주세요.")
            return

        # 1. 파일 다중 선택
        file_paths = filedialog.askopenfilenames(title="변환할 이미지들을 선택하세요")
        if not file_paths: return

        # 2. 저장 폴더 선택
        save_dir = filedialog.askdirectory(title="저장 폴더 선택")
        if not save_dir: return
        save_dir = Path(save_dir)

        # 3. 프로그레스 창 생성 및 처리
        self.run_conversion(file_paths, save_dir)

    def run_conversion(self, file_paths, save_dir):
        # 프로그레스 창 생성
        prog_win = Toplevel(self.root)
        prog_win.title("진행 중")
        prog_win.geometry("400x120")

        # --- 창 순서 및 제어 설정 ---
        prog_win.transient(self.root)   # 메인 창에 종속됨 (항상 메인 창 위에 존재)
        prog_win.grab_set()             # 메인 창의 이벤트를 차단 (작업 중 메인 창 조작 방지)
        prog_win.focus_set()            # 포커스를 이 창으로 가져옴
        # --------------------------

        lbl = Label(prog_win, text="준비 중...")
        lbl.pack(pady=10)
        pb = Progressbar(prog_win, length=300, mode="determinate", maximum=len(file_paths))
        pb.pack()

        # 배경 모드 미리 로드 (성능)
        bg_source = None
        if self.bg_mode.get() == "image":
            bg_source = Image.open(self.bg_image_path).convert("RGB")

        count = 0
        for path_str in file_paths:
            in_path = Path(path_str)
            out_path = save_dir / f"{in_path.stem}_square.jpg"

            lbl.config(text=f"처리 중: {in_path.name}")
            pb["value"] = count + 1
            prog_win.update()

            try:
                with Image.open(in_path) as img:
                    img = ImageOps.exif_transpose(img).convert("RGB")
                    w, h = img.size
                    max_side = max(w, h)

                    # 배경 로직
                    mode = self.bg_mode.get()
                    if mode == "blur":
                        bg = img.resize((max_side//4, max_side//4), Image.NEAREST)
                        bg = bg.filter(ImageFilter.GaussianBlur(15))
                        bg = bg.resize((max_side, max_side), Image.LANCZOS)
                        bg = ImageEnhance.Brightness(bg).enhance(0.4)
                    elif mode == "black":
                        bg = Image.new("RGB", (max_side, max_side), (0, 0, 0))
                    elif mode == "white":
                        bg = Image.new("RGB", (max_side, max_side), (255, 255, 255))
                    elif mode == "custom":
                        bg = Image.new("RGB", (max_side, max_side), self.custom_rgb)
                    elif mode == "image":
                        bg = bg_source.resize((max_side, max_side), Image.LANCZOS)

                    bg.paste(img, ((max_side-w)//2, (max_side-h)//2))
                    bg.save(out_path, "JPEG", quality=95)
                    count += 1
            except Exception as e:
                print(f"Error {in_path.name}: {e}")

        prog_win.destroy()
        messagebox.showinfo("완료", f"{count}개의 작업이 완료되었습니다!")
        os.startfile(str(save_dir))

if __name__ == "__main__":
    root = Tk()
    app = ImageConverterApp(root)
    root.mainloop()