import asyncio
import os
import io
import re
import json
import httpx
from PIL import Image

UA = 'facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)'
HEADERS_BOT = {'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9'}
HEADERS_IMG = {'User-Agent': 'Mozilla/5.0'}

async def fetch_thumb(client, sem, item):
    url = item.get('url', '')
    parts = [p for p in url.split('/') if p]
    if not parts:
        return None
    sc = parts[-1]
    webp_path = os.path.join('thumbs', f'{sc}.webp')
    
    if os.path.exists(webp_path):
        return sc
        
    async with sem:
        try:
            r = await client.get(url, headers=HEADERS_BOT)
            m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text)
            if not m:
                m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text)
            if not m:
                return None
                
            img_url = m.group(1).replace('&amp;', '&')
            img_resp = await client.get(img_url, headers=HEADERS_IMG)
            if img_resp.status_code != 200:
                return None
                
            def process_and_save():
                img = Image.open(io.BytesIO(img_resp.content)).convert('RGB')
                w, h = img.size
                new_w = 360
                new_h = int(h * (360 / w))
                img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
                img_resized.save(webp_path, format='WEBP', quality=75, method=4)
                
            await asyncio.to_thread(process_and_save)
            return sc
        except Exception:
            return None

async def main():
    os.makedirs('thumbs', exist_ok=True)
    with open('trends.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    items = data.get('items', [])
    
    by_xf = sorted(items, key=lambda x: x.get('x_factor') or 0, reverse=True)[:100]
    by_hs = sorted(items, key=lambda x: x.get('hot_score') or 0, reverse=True)[:100]
    by_vw = sorted(items, key=lambda x: x.get('views') or 0, reverse=True)[:100]
    
    seen_ids = set()
    targets = []
    for it in (by_xf + by_hs + by_vw):
        if it['id'] not in seen_ids:
            seen_ids.add(it['id'])
            targets.append(it)
            
    print(f'Total target reels to cache: {len(targets)}')
    sem = asyncio.Semaphore(10)
    
    async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
        tasks = [fetch_thumb(client, sem, item) for item in targets]
        results = await asyncio.gather(*tasks)
        
    saved_scs = {sc for sc in results if sc}
    print(f'Successfully cached {len(saved_scs)}/{len(targets)} thumbnails!')
    
    # Update trends.json items with thumbs paths
    updated_count = 0
    for it in items:
        url = it.get('url', '')
        parts = [p for p in url.split('/') if p]
        if parts:
            sc = parts[-1]
            if os.path.exists(os.path.join('thumbs', f'{sc}.webp')):
                it['thumbnail_url'] = f'thumbs/{sc}.webp'
                updated_count += 1
                
    with open('trends.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
        
    print(f'Updated trends.json with {updated_count} local WebP thumbnails!')

if __name__ == '__main__':
    asyncio.run(main())
