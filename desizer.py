from PIL import Image
import os

folder_name = "0604"
input_folder = "./from/" + folder_name
output_folder = "./to/des/" + folder_name

# 결과 이미지를 저장할 출력 폴더가 없으면 생성
if not os.path.exists(output_folder):
    os.makedirs(output_folder)

# 입력 폴더 내 파일 순회
for filename in os.listdir(input_folder):
    if filename.lower().endswith(".jpg"):
        input_file_path = os.path.join(input_folder, filename)
        output_file_path = os.path.join(output_folder, "des_"+filename)

        with Image.open(input_file_path) as img:
            s1, s2 = img.size
            shorter_side = min(s1, s2)
            longer_side = max(s1, s2)

            aspect_ratio = longer_side / shorter_side
            new_shorter = 1920
            new_longer = int(new_shorter * aspect_ratio)

            if s1 > s2:
                resized_img = img.resize((new_longer, new_shorter), Image.LANCZOS)
            else:
                resized_img = img.resize((new_shorter, new_longer), Image.LANCZOS)
                resized_img = resized_img.rotate(90, expand=True)
            resized_img.save(output_file_path, "JPEG", quality=90)