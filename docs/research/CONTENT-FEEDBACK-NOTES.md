# 内容反馈研究笔记：起量、爆发与长尾

| 项目 | 内容 |
| --- | --- |
| **适用版本** | v5.1.0（演进记录反馈观察） |
| **最后更新** | 2026-10-02 |
| **状态** | 活跃 |
| **说明** | 核对起量与长尾的公开证据，区分平台事实、账号经验和设计假设 |

范围与方法：只核对可公开访问的官方页面，不抓取登录用户数据；跨平台资料仅作设计参考。

## 已核实

1. YouTube 官方性能 FAQ 明确说，发布时间“**is not known to impact a video's
   long-term performance**”；同时说明在受众活跃时发布“**can be beneficial for
   early viewership**”，但“**isn't known to impact a video's long-term
   viewership**”。这是 YouTube 的官方说明，不能据此推导知乎的长期淘汰日。
   来源：[YouTube performance FAQ，When is the best time to publish videos?](https://support.google.com/youtube/answer/141805?hl=en)

2. 同一官方 FAQ 对推荐选择的描述是，系统关注“**What they watch / What they
   don't watch / What they search for / Likes and dislikes / ‘Not interested’
   feedback**”。它还说 Home 的选择取决于相似观众对视频的兴趣和满意度，以及观众的观看/搜索历史。
   这些是 YouTube 对推荐信号的说明；把反馈设计成持续观察的受众匹配信号属于我们的设计推论。
   页面未给出知乎或其他平台的固定淘汰时间。来源：
   [YouTube performance FAQ，How does YouTube choose what videos to promote?](https://support.google.com/youtube/answer/141805?hl=en)

3. YouTube 官方页面把目标表述为帮助观众找到“**the videos they're most likely to
   watch and maximize long-term viewer satisfaction**”。这只说明 YouTube 的推荐目标，
   不能证明知乎采用相同的信号、权重或时间窗。来源：
   [YouTube performance FAQ](https://support.google.com/youtube/answer/141805?hl=en)

4. 同一 FAQ 的旧视频起量问题说明：“**It's common for viewers to start showing more
   interest in old videos**”。列举的可能原因包括主题热度上升、新观众发现频道并观看旧视频、
   观众更愿意点击推荐中的该视频，以及新系列内容带动旧内容。它支持在设计中保留旧内容
   重新起量的观察能力，但不能据此估计知乎旧文章起量的概率。来源：
   [YouTube performance FAQ，An old video recently took off, why?](https://support.google.com/youtube/answer/141805?hl=en)

## 未知与证据边界

- 本次公开核对没有找到知乎官方公开材料支持“发布后 7 天、10 天或 15 天后基本不会再起量”
  这样的硬阈值。不能把这些数字写成知乎平台规则。
- “某账号在第 N 天爆发/长期没有再起量”只能作为账号经验；若没有可复核的官方方法、
  完整样本和控制条件，无法区分内容生命周期、搜索需求变化、外部传播、账号受众或抽样偏差。
- 知乎官方创作者入口 [知乎创作者中心](https://www.zhihu.com/creator) 在未登录公开
  请求中跳转登录；尝试的 [创作者教育入口](https://www.zhihu.com/creator/education)
  返回 404。本次没有取得可直接引用的知乎公开算法或创作者数据说明，因此没有把第三方
  “流量池/几日淘汰”说法当作事实。
- YouTube 资料只能支持一般机制类设计假设，不能当作知乎推荐系统的证明，也不能推出知乎
  存在或不存在任何特定天数阈值。

## 设计含义（待讨论）

- 内容反馈可以按“早期信号”和“长尾信号”分开记录：早期反馈用于观察首轮受众匹配，
  长尾反馈用于观察搜索、收藏、后续分享或主题需求重新出现；两者都不应由固定天数直接归零。
- 写作方案的 reward 若要表达“起量/爆发/长尾”，应奖励可观测的质量与反馈变化，并保留
  时间衰减或窗口定义作为实验参数；不要把 7/10/15 天当作未经证实的平台常数。
- 若要讨论知乎专属结论，下一步需要知乎官方可引用资料或项目自身的、经授权且去标识化的
  纵向数据。当前资料不足以判断知乎故事的典型爆发日、长尾比例或任何统一生命周期。
