# 家里 · 官网静态站

产品宣传首页 / 客服中心 / 下载页三个页面，纯静态 HTML/CSS，没有后端依赖，可以直接部署到任意静态托管。

设计规范见 `omni` 仓库的 [`docs/brand-and-ui-design.md`](https://github.com/Grant-Huang/omni/blob/main/docs/brand-and-ui-design.md) 第 4 节；这三个页面就是那份规范的直接落地实现。

| 文件 | 说明 |
|---|---|
| `index.html` | 产品宣传首页 |
| `support.html` | 客服中心 |
| `download.html` | 下载页（引导打开 Web App / 添加到主屏幕，原生 App 标注"即将上线"） |
| `styles.css` | 共享样式（颜色/字体 token 与 `mobile-demo` 保持一致，不要在这里发明新颜色） |

页面响应式：桌面宽屏和手机窄屏都做了适配（导航栏在窄屏下收起链接、卡片网格自动换行），可以直接用手机浏览器访问。

## 本地预览

```bash
cd marketing-site
python3 -m http.server 8000
```

浏览器打开 `http://localhost:8000`。

## 发布

这是完全静态的三个页面，没有构建步骤，任何静态托管都能直接用：

- **Netlify / Vercel**：把 `marketing-site/` 文件夹拖进 Netlify Drop（https://app.netlify.com/drop），或者在 Vercel/Netlify 里把这个仓库的 `marketing-site` 设为发布目录（Publish directory），不需要 build command。
- **GitHub Pages**：在仓库 Settings → Pages 里把发布源指向 `marketing-site/` 目录。
- **自己的服务器 / Nginx**：把 `marketing-site/` 整个目录拷到静态资源目录下即可，三个页面互相用相对路径引用，不依赖任何服务端接口。

## 现状与下一步

- 下载页的「打开 Web App」按钮目前指向 `https://app.jia.family` 占位域名——这是产品实际部署（`mobile-demo/` 那套页面接入真实后端之后）要绑定的域名，暂时还没有真实内容，上线前需要替换成真实地址。
- 客服中心的分类卡片和联系方式目前是静态文案，还没有接真实的工单系统/在线客服；先把视觉和信息架构定下来，方便后面接客服系统时直接套用现有页面结构。
- 不要在这三个页面里编造真实数据（用户量、响应时长等），产品还没上线，写不出来的地方留白或用真实数据替换，不要编数字。
