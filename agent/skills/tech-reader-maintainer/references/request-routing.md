# Request routing reference

| User intent | First files to inspect | Required checks |
|---|---|---|
| Fix wording or add explanation | one section `content.html` | build + verify; design review when content is added or removed |
| Add, move, rename, or remove a chapter/section | content + owning `module.json` + TOC/anchors + linked IDs | build + verify + TOC/ID/link checks + design review |
| Add image | `src/assets/manifest.json`, target content/CSS | asset audit + build + verify + design review |
| Change theme or global layout | shell CSS/app + design rules | build + verify + full visual review |
| Change navigation/search/lightbox | `src/shell/app.js` and relevant shell HTML | build + verify + interaction review |
| Add a module | new module directory/config + shell/home | build + verify + full visual review |
