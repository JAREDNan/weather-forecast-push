# 第三方许可证声明（THIRD-PARTY LICENSES）

> 更新日期：2026-10-07
> 依据提示词 §3.1 硬性要求：保留原始仓库的 LICENSE 与版权声明，列明来源与许可证。
> 本项目自身许可证**未预设**，由项目所有者（用户）决定后再补充。

---

## 一、运行依赖（pip 安装，代码中 import）

| 依赖 | 许可证 | 来源 / 版权声明 | 用途 |
|---|---|---|---|
| requests | Apache-2.0 | https://github.com/psf/requests（Copyright 2019 Kenneth Reitz） | HTTP 客户端 |
| APScheduler | MIT | https://github.com/agronholm/apscheduler（Copyright 2014-2025 Alex Gronholm） | 每日定时调度 |
| pytest | MIT | https://github.com/pytest-dev/pytest | 测试框架 |

> 以上均为宽松许可（非 GPL/AGPL 系），不产生传染性义务。

## 二、参考副本（只读，未纳入运行代码）

以下仓库克隆于 `G:\气象预报平台_开源参考\`，**仅作接口调用方式参考，其源码未被复制进本项目运行代码**，因此不产生许可证传染问题。

| 仓库 | 许可证 | 本地副本 | 用途 |
|---|---|---|---|
| wechatpy/wechatpy | MIT | `G:\气象预报平台_开源参考\wechatpy_wechatpy\` | 参考微信官方 API 调用方式（token/素材/群发/预览） |
| 版权声明 | Copyright (c) 2014-2020 messense，见副本 `LICENSE` 文件 | | |

> 调研过但未克隆的仓库（EverydayWechat 等）见 `reports/01_开源项目调研.md` 与
> `G:\气象预报平台_开源参考\_清单.md`，均有未克隆原因记录。

## 三、外部数据与接口（非代码许可）

| 对象 | 性质 | 合规说明 |
|---|---|---|
| 中央气象台 nmc.cn / 中国天气网 weather.com.cn | 国家官方公开气象信息 | 公开页面与官方接口，robots 允许；文案署名按要求标注来源（《气象法》第二十五条） |
| 和风天气 QWeather | 商业 API（免费订阅计划） | 需用户持有 API Key；免费版许可用于非商业开发测试，不得声明为自有预报（详见 reports/02） |
| 微信公众号 API | 平台开放接口 | 官方 API，仅使用授权凭证，不涉及第三方个人号协议 |

## 四、使用约定

1. 本目录声明随项目维护：新增任何第三方依赖/副本时，必须同步更新本文件。
2. 若将来引入 GPL/AGPL 系依赖，必须重新评估传染性影响并提示项目所有者。
3. 本项目的 LICENSE（如用户决定开源）与本文档相互独立，分别维护。

## 五、待办（需用户确认）

- [ ] 确定本项目自身许可证（如计划开源建议 MIT / Apache-2.0；不开源则不设 LICENSE 即可）
- [ ] 若后续引入深度学习框架（PyTorch/TensorFlow，均 BSD/Apache 系宽松许可），届时补充登记
