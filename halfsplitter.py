import os
from PIL import Image

# ==============================================================================
# 경로 및 설정 정의
# ==============================================================================
DIR_NAME = "0604"

# 원본 하프프레임 사진들이 들어있는 디렉토리 경로
SRC_DIR = "./from/" + DIR_NAME

# 분할된 개별 사진들을 저장할 디렉토리 경로 (따로 명시)
DST_DIR = "./to/split/" + DIR_NAME

# 하프프레임 단일 컷의 타겟 화면비 (세로:가로 = 4:3 비율 -> 가로/세로 = 0.75)
# 대중적인 하프프레임 스캔 비율에 맞춰 조정 가능합니다. (예: 1:1.333)
TARGET_ASPECT_RATIO = 0.71

# 지원하는 이미지 확장자
VALID_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.tiff', '.tif')

# ==============================================================================
# 이미지 분할 프로세스 실행
# ==============================================================================
def split_half_frame_photos():
    # 저장할 디렉토리가 없으면 생성
    if not os.path.exists(DST_DIR):
        os.makedirs(DST_DIR)
        print(f"📌 저장 경로를 생성했습니다: {DST_DIR}")

    # 소스 디렉토리 내 파일 탐색
    files = [f for f in os.listdir(SRC_DIR) if f.lower().endswith(VALID_EXTENSIONS)]

    if not files:
        print("❌ 처리할 이미지 파일이 소스 경로에 없습니다.")
        return

    print(f"🚀 총 {len(files)}개의 이미지 분할 작업을 시작합니다.")
    print("-" * 50)

    for filename in files:
        file_path = os.path.join(SRC_DIR, filename)
        name, ext = os.path.splitext(filename)

        try:
            with Image.open(file_path) as img:
                # 사진은 가로 방향 고정 (width > height)
                full_width, height = img.size

                # 세로 길이를 기준으로 타겟 화면비에 맞는 단일 컷의 가로 길이 계산
                # 펜탁스 17의 표준 비율(약 4:3)을 적용하여 양 끝단 크롭 영역 정의
                single_width = int(height * TARGET_ASPECT_RATIO)

                # 스캔 공정이나 마운트 오차로 인한 이미지 깨짐을 방지하기 위해
                # 정확히 이미지의 좌측 끝(0)과 우측 끝(full_width)을 기준으로 크롭 영역을 잡습니다.

                # 왼쪽 컷 (_L) 영역: (left, upper, right, lower)
                left_box = (0, 0, single_width, height)
                # 오른쪽 컷 (_R) 영역
                right_box = (full_width - single_width, 0, full_width, height)

                # 이미지 크롭 및 저장
                left_img = img.crop(left_box)
                right_img = img.crop(right_box)

                left_output_path = os.path.join(DST_DIR, f"{name}_L{ext}")
                right_output_path = os.path.join(DST_DIR, f"{name}_R{ext}")

                # 원본의 EXIF 데이터 유지하며 저장 (존재할 경우)
                exif_data = img.info.get('exif')

                if exif_data:
                    left_img.save(left_output_path, exif=exif_data, quality=95)
                    right_img.save(right_output_path, exif=exif_data, quality=95)
                else:
                    left_img.save(left_output_path, quality=95)
                    right_img.save(right_output_path, quality=95)

                print(f"✅ 분할 완료: {filename} -> _L, _R 저장 완료")

        except Exception as e:
            print(f"❌ 파일 처리 실패 ({filename}): {e}")

    print("-" * 50)
    print("🎉 모든 작업이 완료되었습니다!")

if __name__ == "__main__":
    split_half_frame_photos()