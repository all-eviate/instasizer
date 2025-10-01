import os
from PIL import Image, ImageFilter, ImageEnhance, ImageOps
import pillow_heif  # Pillow + HEIC 지원 (pillow-heif 라이브러리 사용)

# pillow-heif를 통해 HEIC 이미지 지원 활성화
pillow_heif.register_heif_opener()

# 입력과 출력 폴더 경로
input_folder = "./from"
output_folder = "./to"

# blur, black, white, image, custom
bg_mode = "white"
blur_radius = 20
brightness_factor = 0.4
colour_r = 0
colour_g = 0
colour_b = 0

# 결과 이미지를 저장할 출력 폴더가 없으면 생성
if not os.path.exists(output_folder):
    os.makedirs(output_folder)

# 입력 폴더 내 파일 순회
for filename in os.listdir(input_folder):
    # HEIC 파일만 처리 (.heic 확장자 확인)
    if filename.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".gif", ".heic")):
        input_file_path = os.path.join(input_folder, filename)
        output_file_path = os.path.join(output_folder, f"{os.path.splitext(filename)[0]}_{bg_mode}_square.jpg")

        # HEIC 이미지를 열고 RGB로 변환
        with Image.open(input_file_path) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            width, height = img.size
            max_side = max(width, height)
            min_side = min(width, height)

            if bg_mode == "blur":
                # 1. 배경 이미지를 만들기 위해 이미지를 정방형으로 확대 및 크롭
                background = img.copy()
                scale_factor = max_side / min_side  # 짧은 변을 길이에 맞추기 위해 확대 비율 계산

                if width < height:  # 가로가 더 짧을 경우
                    new_width = max_side
                    new_height = int(height * scale_factor)
                else:  # 세로가 더 짧을 경우
                    new_width = int(width * scale_factor)
                    new_height = max_side

                # 이미지 크기 조정 (resize) 후 중앙 크롭
                background = background.resize((new_width, new_height), Image.LANCZOS)
                left = (new_width - max_side) // 2
                top = (new_height - max_side) // 2
                background = background.crop((left, top, left + max_side, top + max_side))

                # 2. 배경 이미지에 Blur 필터 적용
                background = background.filter(ImageFilter.GaussianBlur(blur_radius))  # 흐림 효과 강도 조정 가능

                enhancer = ImageEnhance.Brightness(background)
                background = enhancer.enhance(brightness_factor)
            elif bg_mode == "black":
                background = Image.new("RGB", (max_side, max_side), (0, 0, 0))
            elif bg_mode == "white":
                background = Image.new("RGB", (max_side, max_side), (255, 255, 255))
            elif bg_mode == "image":
                background = Image.open("./background.jpg")
            elif bg_mode == "custom":
                background = Image.new("RGB", (max_side, max_side), (colour_r, colour_g, colour_b))

            # 3. 정방형 캔버스 생성 (배경 이미지를 추가)
            square_img = background

            # 4. 원본 이미지를 배경 이미지의 중앙에 붙이기
            x_offset = (max_side - width) // 2
            y_offset = (max_side - height) // 2
            square_img.paste(img, (x_offset, y_offset))

            # 5. 결과 이미지를 저장
            square_img.save(output_file_path, "JPEG")

print("이미지 변환이 완료되었습니다.")
