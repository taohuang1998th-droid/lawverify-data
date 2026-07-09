# lawverify-data

[LawVerify Pro](https://github.com/taohuang1998th-droid/lawverify-pro) 浏览器扩展 / Word 插件的**增量法条数据**，每日由 CI 自动抓取更新。

- `latest_laws.json` — 最近时间窗口内新发布法律条文的 patch 列表
  （格式：`{ "version": "YYYY-MM-DD", "patches": [...] }`）
- 数据来源：最高人民法院官网司法解释专栏等公开渠道
- 法律条文属公开的公共信息；本仓库仅作分发镜像，供客户端免密钥拉取

客户端读取地址：

    https://raw.githubusercontent.com/taohuang1998th-droid/lawverify-data/main/latest_laws.json
