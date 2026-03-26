import os
import sys
import logging
import requests
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
import openai
from rembg import remove
from tqdm import tqdm
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_TABLE = "foods"
SUPABASE_BUCKET = "food-images" # 이미지를 저장할 버킷 이름 (직접 생성하셔야 합니다)

# Variables
INPUT_FILE = "foods.txt"
OUTPUT_DIR = "output"
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/nanumgothic/NanumGothic-Bold.ttf"
FONT_PATH = "NanumGothic-Bold.ttf"

# Colors extracted from the reference image
BLUE_BG = (59, 62, 122) # #3B3E7A
WHITE_BG = (242, 240, 230) # #F2F0E6

CARD_WIDTH = 1080
CARD_HEIGHT = 1350

def download_font():
    if not os.path.exists(FONT_PATH):
        logging.info("Downloading font (NanumGothic-Bold.ttf)...")
        r = requests.get(FONT_URL)
        if r.status_code == 200:
            with open(FONT_PATH, "wb") as f:
                f.write(r.content)
            logging.info("Font downloaded successfully.")
        else:
            logging.error("Failed to download font.")
            sys.exit(1)

def ensure_output_dir():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

def read_foods():
    if not os.path.exists(INPUT_FILE):
        logging.error(f"{INPUT_FILE} does not exist. Please create it and add food names (one per line).")
        with open(INPUT_FILE, "w", encoding="utf-8") as f:
            f.write("Coffee\nCake\nScone")
        logging.info("Created a sample foods.txt file for you.")
    ~``
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        foods = [line.strip() for line in f if line.strip()]
        
    if len(foods) > 100:
        logging.warning("More than 100 foods given, truncating to 100.")
        foods = foods[:100]
    return foods

def get_food_image(food_name, client):
    # DALL-E 3 request for a clean, photorealistic food picture
    prompt = f"A photorealistic, highly detailed studio photograph of {food_name}, perfectly isolated on a solid gray background without any severe shadows. Professional food photography."
    res = client.images.generate(
        model="dall-e-3",
        prompt=prompt,
        size="1024x1024",
        quality="standard",
        n=1
    )
    img_url = res.data[0].url
    img_res = requests.get(img_url)
    img = Image.open(BytesIO(img_res.content)).convert("RGBA")
    return img

def create_card(food_img_nobg, food_name, bg_color, text_color, font, suffix):
    # Canvas
    card = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), bg_color)
    
    # Process food img
    # Crop to actual content to remove excessive transparent space
    bbox = food_img_nobg.getbbox()
    if bbox:
        food_img_nobg = food_img_nobg.crop(bbox)
        
    # Resize so food fits tightly into an 700x700 square area
    max_food_w, max_food_h = 700, 700
    ratio = min(max_food_w / float(food_img_nobg.width), max_food_h / float(food_img_nobg.height))
    
    new_size = (int(food_img_nobg.width * ratio), int(food_img_nobg.height * ratio))
    # Resize the image using Lanczos
    food_img_nobg = food_img_nobg.resize(new_size, Image.Resampling.LANCZOS)
        
    # Center the food.
    paste_x = (CARD_WIDTH - food_img_nobg.width) // 2
    paste_y = (CARD_HEIGHT - food_img_nobg.height) // 2
    
    # Use food image alpha channel as mask when pasting
    card.paste(food_img_nobg, (paste_x, paste_y), food_img_nobg)
    
    # Add text "[ Food Name ]"
    draw = ImageDraw.Draw(card)
    text = f"[ {food_name} ]"
    
    # textbbox returns (left, top, right, bottom)
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_w = text_bbox[2] - text_bbox[0]
    
    # Coordinates for the text
    text_x = (CARD_WIDTH - text_w) // 2
    text_y = 1080  # Consistent baseline
    
    draw.text((text_x, text_y), text, font=font, fill=text_color)
    
    # Save optimized as WebP
    card = card.convert("RGB")
    
    # Replace spaces with underscores for filenames
    safe_food_name = food_name.replace(" ", "_").lower()
    save_path = os.path.join(OUTPUT_DIR, f"{safe_food_name}_{suffix}.webp")
    
    # WebP with quality 80% should easily guarantee < 200KB for non-complex images
    card.save(save_path, "WEBP", quality=80)
    
    # Log warning if it exceeds 200KB limit
    if os.path.exists(save_path) and os.path.getsize(save_path) > 200 * 1024:
        logging.warning(f"File {save_path} is over 200KB. Attempting strict compression.")
        card.save(save_path, "WEBP", quality=65)
        
    return save_path

def main():
    # 터미널에서 입력하셨던 키를 자동으로 환경변수에 설정합니다.
    # (깃허브 유출 방지를 위해 .env 파일에서 가져오도록 변경되었습니다)
    
    if not os.getenv("OPENAI_API_KEY"):
        logging.error("OPENAI_API_KEY is not set.")
        sys.exit(1)
        
    client = openai.OpenAI()
    
    download_font()
    ensure_output_dir()
    
    print("\n[ 옵션 선택 ]")
    print("1: 직접 음식 이름 입력 (또는 foods.txt 사용)")
    print("2: Supabase에서 안 채워진 데이터 가져오기 (image_url_white/blue 기준)")
    choice = input("입력: ")

    supabase = None
    foods_data = []

    if choice.strip() == "2":
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        try:
            # image_url_white가 null인 데이터를 우선 찾습니다. (이 컬럼들을 먼저 생성하셨다고 가정합니다)
            res = supabase.table(SUPABASE_TABLE).select("id, name").is_("image_url_white", "null").execute()
            foods_data = res.data
        except Exception as e:
            logging.error(f"Supabase 조회 실패: {e}")
            return
            
        if not foods_data:
            logging.info("Supabase에 이미지가 필요한 데이터가 없습니다.")
            return
        logging.info(f"Supabase에서 가져온 음식 수: {len(foods_data)}")
    else:
        user_input = input("만들고 싶은 음식의 이름을 입력하세요 (여러 개인 경우 쉼표로 구분, 빈칸 입력 시 foods.txt 사용): ")
        if user_input.strip():
            raw_foods = [f.strip() for f in user_input.split(",") if f.strip()]
        else:
            raw_foods = read_foods()
        
        # 일관된 처리를 위해 dict 형태로 만듬
        foods_data = [{"id": f.replace(" ", "_").lower(), "name": f} for f in raw_foods]
        
    if not foods_data:
        logging.info("No foods to process. Please add some!")
        return
        
    try:
        font = ImageFont.truetype(FONT_PATH, 75)
    except IOError:
        logging.error("Failed to load font. Aborting.")
        sys.exit(1)
    
    for item in tqdm(foods_data, desc="Generating Cards"):
        food_id = item["id"]
        food_name = item["name"]
        
        try:
            # 1. Generate food image using DALL-E 3
            img = get_food_image(food_name, client)
            
            # 2. Remove background
            img_nobg = remove(img)
            
            # 3. Create cards
            b_path = create_card(img_nobg, food_name, BLUE_BG, WHITE_BG, font, "blue")
            w_path = create_card(img_nobg, food_name, WHITE_BG, BLUE_BG, font, "white")
            
            # 4. Upload to Supabase and update column
            if choice.strip() == "2" and supabase:
                try:
                    with open(w_path, "rb") as f:
                        supabase.storage.from_(SUPABASE_BUCKET).upload(f"cards/{food_id}_white.webp", f, {"content-type": "image/webp", "upsert": "true"})
                    with open(b_path, "rb") as f:
                        supabase.storage.from_(SUPABASE_BUCKET).upload(f"cards/{food_id}_blue.webp", f, {"content-type": "image/webp", "upsert": "true"})
                        
                    w_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(f"cards/{food_id}_white.webp")
                    b_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(f"cards/{food_id}_blue.webp")
                    
                    # 업데이트
                    supabase.table(SUPABASE_TABLE).update({
                        "image_url_white": w_url,
                        "image_url_blue": b_url
                    }).eq("id", food_id).execute()
                    logging.info(f"Supabase 업데이트 완료: {food_name}")
                except Exception as upload_e:
                    logging.error(f"Supabase 업로드/업데이트 실패: {upload_e}")
            
        except Exception as e:
            logging.error(f"Error processing '{food_name}': {e}")
            
    logging.info("All finished!")

if __name__ == "__main__":
    main()
