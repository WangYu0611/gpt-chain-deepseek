"""Produce the local review handoff from verified UE screenshots and batch receipts."""
from pathlib import Path
import html
import json
import sys

def build(root):
    root = Path(root)
    report = json.loads((root / "review-package.json").read_text(encoding="utf-8"))
    budget = report["budget"]["budget"]
    shots = {s["name"]: s for s in report["capture"]["shots"]}
    def photo(name, caption):
        image = Path(shots[name]["path"]).relative_to(root).as_posix()
        return f'<figure><a href="{html.escape(image)}" target="_blank"><img src="{html.escape(image)}" alt="{html.escape(caption)}" loading="lazy"></a><figcaption>{html.escape(caption)}</figcaption></figure>'
    cards = []
    for level, label, number, description in (
        ("L1", "初居 · 茅顶土墙", "02", "朴素材料的完整细节、生活院地与统一人物尺度。"),
        ("L4", "成院 · 青瓦木作", "03", "房屋与院墙组织、铺地和附属生活设施。"),
        ("L8", "丰居 · 两层主屋与附房", "04", "两层主屋、廊檐与独立附属用房，保持普通民居身份。")):
        cards.append('<section><div class="section-title"><span>'+level+'</span><div><h2>'+label+'</h2><p>'+description+'</p></div></div>'+
                     photo(number+'_'+level+'_courtyard',level+' · UE 5.8 实际院落展示')+
                     photo({'L1':'05','L4':'06','L8':'07'}[level]+'_'+level+'_details',level+' · 近景细节')+'</section>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>普通住宅 · 首轮美术预览</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f0e8;color:#292d28;font-family:"Microsoft YaHei",sans-serif;line-height:1.75}main{max-width:1320px;margin:auto;padding:50px 34px}header{border-top:5px solid #556b58;padding-top:26px}.eyebrow{letter-spacing:.15em;color:#6c766b;font-size:12px}h1{font-size:40px;margin:14px 0 6px;font-weight:600}h2{font-size:24px;margin:0}p{margin:8px 0 18px;color:#62685f}.badge{display:inline-block;padding:4px 12px;background:#e6ddbf;border-radius:20px;font-size:13px}.facts{display:flex;flex-wrap:wrap;gap:12px;margin:25px 0 35px}.facts div{background:#fffdf7;padding:16px 24px;min-width:190px;border:1px solid #dddccd}.facts strong{display:block;font-size:24px}.facts small{color:#72786e}section{margin:48px 0}.section-title{display:flex;align-items:center;gap:18px;margin-bottom:18px}.section-title>span{font:38px Georgia;color:#72816a}figure{margin:18px 0;background:#e9e5dc}img{display:block;width:100%;height:auto}figcaption{padding:12px 18px;color:#60675c;font-size:13px}nav{display:flex;gap:20px;flex-wrap:wrap;border-bottom:1px solid #cdd1c4;padding:20px 0}a{color:#3c644c}footer{border-top:1px solid #cdd1c4;padding:28px 0;color:#72786e;font-size:13px}.notice{background:#fffdf7;border-left:4px solid #aa9366;padding:18px 22px}li{margin:8px 0}@media(max-width:700px){main{padding:25px 16px}h1{font-size:28px}}
</style><main><header><div class="eyebrow">ZHUGUO CITY / HOUSE-COMMON-001 / A-REFINED</div><h1>普通住宅 · 三级美术预览</h1><p>半写实手绘 A · 固定大地块 · 从朴素初居到完整院宅</p><span class="badge">待你确认视觉方向 · 其余五级尚未制作</span></header>'''
    page += f'<div class="facts"><div><small>本轮展示</small><strong>L1 / L4 / L8</strong></div><div><small>Tripo 已消耗</small><strong>{budget["actual"]:g} / 3000 积分</strong></div><div><small>可用批次预算</small><strong>{budget["remaining"]:g} 积分</strong></div><div><small>展示地块</small><strong>16 × 16 米</strong></div></div>'
    page += '<div class="notice">这是实际生成模型、Blender 处理后在 UE 5.8 中拍摄的预览。地块尺寸用于本轮比较，可在确认前调整；正式游戏的住宅占地与升级表现尚未替换。人形为 1.8 米尺度参照。生活道具采用本地程序建模，不额外消耗 Tripo 积分。</div>'
    page += '<nav><a href="review-package.json">完整制作与验证回执</a><a href="art-brief.json">批次要求与参考版本</a><a href="UE/HousingPreview/HousingPreview.uproject">UE 预览工程</a></nav>'
    page += '<section><h2>同一尺度下的等级变化</h2>'+photo('01_all_levels','从左到右：L8 高级、L4 中级、L1 低级 · 统一光照与尺度；点击图片查看原图')+'</section>'
    page += ''.join(cards)
    page += '<section><h2>背面与结构检查</h2>'+photo('08_rear_structure','背面视角 · 检查屋顶、墙面与院落完整性')+'</section>'
    page += '<section><details><summary>展开已确认的 Claude A 风格参考</summary><figure><img src="references/A_style_UE_city.png" alt="已确认的 Claude A 风格参考" loading="lazy"><figcaption>原测试工程的 A 风格截图，仅作画风参考；上方八张为本轮实际产物。</figcaption></figure></details></section>'
    page += '<section><h2>这次请重点看</h2><ol><li>A 风格是否保留，同时达到你希望的精细程度。</li><li>低、中、高等级的轮廓、体量和院落组织是否有清楚差异。</li><li>低级住宅是否朴素但精致，生活道具是否自然。</li><li>16 米地块是否合适；需要调整的具体等级或部位。</li></ol><p>确认后，合格的三个样板继续复用，再补齐其余五级。技术检查通过不会自动视为你已确认美术。</p></section>'
    page += '<footer>图像为真实 UE 渲染；概念图、原模型、Blender 源文件、贴图、材质及哈希记录保留在本批次目录。当前为独立展示验证，尚不代表整座城市的性能验收。</footer></main></html>'
    (root / "住宅美术预览.html").write_text(page, encoding="utf-8")
    print(root / "住宅美术预览.html")

if __name__ == "__main__":
    build(sys.argv[1])
