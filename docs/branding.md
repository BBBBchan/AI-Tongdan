# AI同担品牌与小担形象

## 小担

小担是 AI同担的角色形象：黑色双马尾、红瞳、红黑耳机，搭配白领、红领带和黑色袖套。README 使用既有介绍片与日记制作中的角色素材，分别表现欢迎、接梗和专注工作。

| 素材 | 表情与用途 |
| --- | --- |
| [招手的小担](assets/xiaodan/wave.png) | README 顶部主视觉，与 B 站账号入口连接。 |
| [眨眼的小担](assets/xiaodan/smug.png) | 弹幕互动与聊天介绍。 |
| [工作中的小担](assets/xiaodan/focused.png) | 切片与内容制作介绍。 |

三张均为 1254 × 1254 的透明 PNG，直接复用已确认的角色原图，没有重新生成或修改像素。当前 README 顶图显示宽度为 240 px，功能卡片为 130 px。来源与校验值见 [素材记录](assets/xiaodan/README.md)。

## 应用 Logo

两枚并肩的弹幕气泡共享一个播放三角：气泡对应直播互动与同担陪伴，播放三角对应一起观看直播，也呼应 DeltaStreamer 的三角形意象。

配色沿用应用现有的薄荷绿、浅蓝和深海军蓝。图标不包含文字，项目名称通过网页和 README 的文字单独呈现，方便小尺寸展示和辅助阅读。

## 资产与应用

- [Logo PNG 原图](../static/brand/ai-tongdan-logo.png)：1254 × 1254，带透明通道，保留生成工具的原始输出。
- GitHub 仓库 README 目前采用上方的小担角色形象；应用 Logo 保留用于产品界面。
- Web 应用页头：桌面 40 px、手机 36 px，由浏览器按比例缩放。
- 浏览器 favicon 与 Apple touch icon：引用同一张 PNG。
- 尚未连接直播时的播放器占位图：使用同一标志。

图标来自项目内的静态资源，无需外部图片服务。原创项目资产适用仓库根目录的 [CC BY-NC 4.0](../LICENSE)。

## 生成记录

使用内置 **image_gen** 工具生成；没有使用 CLI/API 后备路径。原始 PNG 复制到项目中后直接应用，没有手工修改像素。最终生成提示词如下：

```text
Use case: logo-brand.
Asset type: final square app/repository logo icon for “AI同担” (AI-Tongdan), an AI companion that watches Bilibili/VTuber livestreams with a fan, understands visuals, audio and audience chat, remembers moments and answers questions.
Primary request: design ONE original, polished, immediately recognizable logo. Concept: two interlocking rounded speech bubbles sitting together like two fans watching the same stream, sharing a single bold play-triangle in their central negative space. The paired bubbles should suggest companionship and subtly suggest the two lobes of a heart without adding a separate heart. The play triangle also quietly nods to the DeltaStreamer origin. Keep the geometry intentional and the silhouette extremely simple.
Style: premium flat geometric brand symbol, crisp vector-like edges, balanced thick forms, friendly and understated, readable as a 32-pixel favicon. No text or letters.
Color palette: existing application colors: mint #ADEDD3 and soft periwinkle-blue #91B4F4, with midnight navy #0B1017. Two main solid color shapes, no gradients.
Composition: square 1024×1024 image, a midnight-navy rounded-square tile filling about 94 percent of the canvas, centered mark filling about 72 percent of the tile. Entire tile visible with symmetrical modest padding. Genuinely transparent pixels outside the rounded tile. Clean edges, actual alpha, no checkerboard pattern.
Constraints: one logo only, front-on, no mockup or presentation board, no extra variants, no decorative stars, no circuitry, no robot-head cliche, no Bilibili mascot imitation, no watermark, no shadows, no 3D, no tiny details. Deliver a finished icon suitable for a GitHub README, web app header and browser favicon.
```
