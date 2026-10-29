/* 前后端共享的枚举与中文标签。 */

export const USAGE_MODES = [
  { value: 'facts', label: '事实依据', desc: '只取事实与设定，不改写原文' },
  { value: 'style', label: '文风样本', desc: '学习句式、节奏与用词，不复制内容' },
  { value: 'structure', label: '结构参考', desc: '参考章节组织与节拍' },
  { value: 'adapt', label: '改编素材', desc: '允许改写情节与人物关系' }
];

export function usageLabel(value) {
  const hit = USAGE_MODES.find((m) => m.value === value);
  return hit ? hit.label : '事实依据';
}

export const GENRE_OPTIONS = ['悬疑', '古言', '都市', '玄幻', '科幻', '武侠', '剧本', '诗词', '散文'];

export const SCALE_OPTIONS = [
  { value: 'short', label: '短篇', chapters: 100, desc: '约 30 万字 · 100 章' },
  { value: '1m', label: '百万字', chapters: 300, desc: '约 100 万字 · 300 章' },
  { value: '3m', label: '三百万字', chapters: 900, desc: '约 300 万字 · 900 章' },
  { value: '5m', label: '五百万字', chapters: 1500, desc: '约 500 万字 · 1500 章' }
];

export function scaleLabel(value) {
  const hit = SCALE_OPTIONS.find((s) => s.value === value);
  return hit ? hit.label : value || '';
}

export const WRITING_MODES = [
  { value: 'rolling', label: '滚动规划', desc: '写一段、看反馈、再规划，适合连载。', icon: 'refresh' },
  { value: 'outline', label: '大纲优先', desc: '先定全书骨架，再逐章落地。', icon: 'list' },
  { value: 'one_shot', label: '一次成稿', desc: '适合短篇，一次生成再统一打磨。', icon: 'zap' }
];

export const MEDIUM_FALLBACK = [
  { key: 'novel', label: '小说', icon: 'book', desc: '长篇连载、中篇与短篇集。' },
  { key: 'script', label: '剧本', icon: 'film', desc: '长片、剧集、短片、舞台与戏曲。' },
  { key: 'poetry', label: '诗词', icon: 'feather', desc: '格律诗、词牌、古风与现代诗。' },
  { key: 'prose', label: '散文', icon: 'pen', desc: '抒情、叙事、哲理与游记。' },
  { key: 'thesis', label: '论文', icon: 'file-text', desc: '学术论文：提纲、引用文献、降 AIGC。' },
  { key: 'children', label: '儿童文学', icon: 'book', desc: '寓言、绘本故事、儿童科幻与科学故事。' },
  { key: 'news', label: '新闻稿', icon: 'globe', desc: '产品发布、官方公告、活动通稿。' },
  { key: 'copy', label: '文案', icon: 'zap', desc: '口播稿、种草笔记、宣传与营销文案。' }
];

/* 后端 /api/mediums 的 icon 是 emoji（旧版界面直接渲染），v2 一律按体裁 key 取 SVG 名。 */
const MEDIUM_ICON = { novel: 'book', script: 'film', poetry: 'feather', prose: 'pen', thesis: 'file-text', children: 'book', news: 'globe', copy: 'zap' };

export function mediumIcon(medium) {
  const key = typeof medium === 'string' ? medium : (medium && medium.key) || '';
  return MEDIUM_ICON[key] || 'book';
}

export const FEEDBACK_TAGS = ['剧情拖沓', '人物失真', '文风不对', '信息错误', '没按规划', '太像 AI'];
