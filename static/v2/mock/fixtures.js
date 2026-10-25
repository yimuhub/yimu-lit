/* 真实感演示夹具：?mock=1 时使用，无后端、无 LLM 也能走通全部界面。 */

const HOUR = 3600 * 1000;
const now = Date.now();
const iso = (offsetMs) => new Date(now - offsetMs).toISOString().slice(0, 19);

const VOLUME_TITLES = ['长夜行舟', '灯河暗涌', '无声之火', '天明之前'];
const ARC_TITLES = [
  '失踪的守灯人', '第七码头', '雨夜的名单', '旧报纸上的名字', '第一次跟踪',
  '灯塔之下', '假证人', '潮汐档案', '被烧掉的航线', '线人失踪',
  '内鬼', '凌晨三点的电话'
];
const CHAPTER_TITLES = [
  '雨落在铁皮屋顶', '最后一班渡轮', '灯塔看守人', '没有寄出的信', '第七码头的雾',
  '旧报纸', '一块停在十一点的表', '沉默的证人', '暗号', '夜航图',
  '潮湿的档案袋', '第二次敲门', '名单上少了一个人', '灯河', '守夜者',
  '空椅子', '迟到的供词', '一场没有雨的雨夜', '烧焦的角落', '半枚指纹',
  '码头以南', '失约', '雾里的船笛', '第三个名字', '凌晨的收音机',
  '被改过的潮汐表', '灯塔名单', '旧钟楼', '一个人的证词', '天亮之前'
];

function chapterTitle(no) {
  return CHAPTER_TITLES[(no - 1) % CHAPTER_TITLES.length];
}

function buildChapters(count = 120, done = 46) {
  const list = [];
  for (let i = 1; i <= count; i += 1) {
    let status = 'pending';
    if (i <= done) status = 'completed';
    else if (i === done + 1) status = 'in_progress';
    const words = i <= done ? 2800 + ((i * 137) % 420) : 0;
    list.push({
      chapterNumber: i,
      title: `第${i}章 ${chapterTitle(i)}`,
      filePath: `第${String(i).padStart(2, '0')}章-${chapterTitle(i)}.md`,
      status,
      wordCount: words,
      wordCountPass: i <= done,
      retryCount: i % 17 === 0 ? 1 : 0
    });
  }
  return list;
}

const demoChapters = buildChapters(120, 46);

const BOOKS = [
  {
    key: '20261021-104233-长夜灯河',
    name: '长夜灯河',
    genre: '悬疑',
    state: 'in_progress',
    status_label: '创作中',
    done: 46,
    total: 120,
    total_words: 146800,
    running: true,
    quality: 88.5,
    passing: true,
    pass_line: 85,
    released: false,
    created_at: iso(16 * 24 * HOUR),
    updated_at: iso(2 * 60 * 1000),
    favorite: true,
    tags: ['双线', '港城', '群像'],
    archived: false
  },
  {
    key: '20260912-220118-山河故人',
    name: '山河故人',
    genre: '古言',
    state: 'in_progress',
    status_label: '创作中',
    done: 134,
    total: 300,
    total_words: 412000,
    running: false,
    quality: 86.2,
    passing: true,
    pass_line: 85,
    released: false,
    created_at: iso(42 * 24 * HOUR),
    updated_at: iso(5 * HOUR),
    favorite: false,
    tags: ['权谋', '慢热'],
    archived: false
  },
  {
    key: '20261003-091500-雾港来信',
    name: '雾港来信',
    genre: '剧本',
    state: 'completed',
    status_label: '已出书',
    done: 24,
    total: 24,
    total_words: 98000,
    running: false,
    quality: 91.3,
    passing: true,
    pass_line: 85,
    released: true,
    created_at: iso(22 * 24 * HOUR),
    updated_at: iso(26 * HOUR),
    favorite: true,
    tags: ['剧集', '12集'],
    archived: false
  },
  {
    key: '20260928-142230-纸上还乡',
    name: '纸上还乡',
    genre: '散文',
    state: 'completed',
    status_label: '已出书',
    done: 12,
    total: 12,
    total_words: 23400,
    running: false,
    quality: 93.1,
    passing: true,
    pass_line: 85,
    released: true,
    created_at: iso(26 * 24 * HOUR),
    updated_at: iso(3 * 24 * HOUR),
    favorite: false,
    tags: ['故乡', '非虚构'],
    archived: false
  },
  {
    key: '20261019-174820-秋声集',
    name: '秋声集',
    genre: '诗词',
    state: 'in_progress',
    status_label: '创作中',
    done: 18,
    total: 30,
    total_words: 6400,
    running: false,
    quality: null,
    passing: null,
    pass_line: 85,
    released: false,
    created_at: iso(6 * 24 * HOUR),
    updated_at: iso(9 * HOUR),
    favorite: false,
    tags: ['七律', '节气'],
    archived: false
  },
  {
    key: '20260830-161026-九万里风',
    name: '九万里风',
    genre: '玄幻',
    state: 'in_progress',
    status_label: '创作中',
    done: 62,
    total: 100,
    total_words: 198000,
    running: false,
    quality: 79.4,
    passing: false,
    pass_line: 85,
    released: false,
    created_at: iso(55 * 24 * HOUR),
    updated_at: iso(2 * 24 * HOUR),
    favorite: false,
    tags: ['修真', '群像'],
    archived: false
  },
  {
    key: '20261024-083012-午夜修理铺',
    name: '午夜修理铺',
    genre: '都市',
    state: 'idle',
    status_label: '草稿',
    done: 3,
    total: 60,
    total_words: 9100,
    running: false,
    quality: null,
    passing: null,
    pass_line: 85,
    released: false,
    created_at: iso(28 * HOUR),
    updated_at: iso(28 * HOUR),
    favorite: false,
    tags: ['日常', '治愈'],
    archived: false
  },
  {
    key: '20261022-193344-最后一个说书人',
    name: '最后一个说书人',
    genre: '剧本',
    state: 'awaiting_title',
    status_label: '选书名',
    done: 0,
    total: 24,
    total_words: 0,
    running: false,
    quality: null,
    passing: null,
    pass_line: 85,
    released: false,
    created_at: iso(3 * 24 * HOUR),
    updated_at: iso(3 * 24 * HOUR),
    favorite: false,
    tags: ['舞台剧'],
    archived: false
  },
  {
    key: '20260905-112000-雪线以下',
    name: '雪线以下',
    genre: '悬疑',
    state: 'failed',
    status_label: '失败',
    done: 21,
    total: 80,
    total_words: 61000,
    running: false,
    quality: 74.8,
    passing: false,
    pass_line: 85,
    released: false,
    created_at: iso(50 * 24 * HOUR),
    updated_at: iso(9 * 24 * HOUR),
    favorite: false,
    tags: ['雪山', '本格'],
    archived: false
  },
  {
    key: '20260811-094500-旧城十二时',
    name: '旧城十二时',
    genre: '散文',
    state: 'completed',
    status_label: '已出书',
    done: 12,
    total: 12,
    total_words: 18600,
    running: false,
    quality: 88.0,
    passing: true,
    pass_line: 85,
    released: true,
    created_at: iso(74 * 24 * HOUR),
    updated_at: iso(31 * 24 * HOUR),
    favorite: false,
    tags: ['城市', '观察'],
    archived: true
  },
  {
    key: '20260722-224410-银河渡口',
    name: '银河渡口',
    genre: '科幻',
    state: 'completed',
    status_label: '已出书',
    done: 60,
    total: 60,
    total_words: 188000,
    running: false,
    quality: 89.6,
    passing: true,
    pass_line: 85,
    released: true,
    created_at: iso(96 * 24 * HOUR),
    updated_at: iso(52 * 24 * HOUR),
    favorite: true,
    tags: ['硬科幻', '远未来'],
    archived: false
  },
  {
    key: '20260618-133020-槐花巷',
    name: '槐花巷',
    genre: '都市',
    state: 'in_progress',
    status_label: '创作中',
    done: 18,
    total: 60,
    total_words: 48200,
    running: false,
    quality: 82.1,
    passing: false,
    pass_line: 85,
    released: false,
    created_at: iso(130 * 24 * HOUR),
    updated_at: iso(64 * 24 * HOUR),
    favorite: false,
    tags: ['年代', '群像'],
    archived: false
  },
  {
    key: '20260602-090011-刀锋与麦田',
    name: '刀锋与麦田',
    genre: '武侠',
    state: 'completed',
    status_label: '已出书',
    done: 40,
    total: 40,
    total_words: 121000,
    running: false,
    quality: 87.4,
    passing: true,
    pass_line: 85,
    released: true,
    created_at: iso(146 * 24 * HOUR),
    updated_at: iso(88 * 24 * HOUR),
    favorite: false,
    tags: ['江湖', '复仇'],
    archived: false
  },
  {
    key: '20260511-211233-南风知我意',
    name: '南风知我意',
    genre: '古言',
    state: 'completed',
    status_label: '已出书',
    done: 80,
    total: 80,
    total_words: 236000,
    running: false,
    quality: 90.2,
    passing: true,
    pass_line: 85,
    released: true,
    created_at: iso(168 * 24 * HOUR),
    updated_at: iso(102 * 24 * HOUR),
    favorite: true,
    tags: ['宫斗', '双强'],
    archived: false
  },
  {
    key: '20260420-121200-雨夜出租车',
    name: '雨夜出租车',
    genre: '悬疑',
    state: 'completed',
    status_label: '已出书',
    done: 24,
    total: 24,
    total_words: 68000,
    running: false,
    quality: 92.5,
    passing: true,
    pass_line: 85,
    released: true,
    created_at: iso(188 * 24 * HOUR),
    updated_at: iso(140 * 24 * HOUR),
    favorite: false,
    tags: ['示例书', '本格'],
    archived: false
  },
  {
    key: '20260315-101010-白鹭洲',
    name: '白鹭洲',
    genre: '诗词',
    state: 'completed',
    status_label: '已出书',
    done: 24,
    total: 24,
    total_words: 5200,
    running: false,
    quality: 95.0,
    passing: true,
    pass_line: 85,
    released: true,
    created_at: iso(224 * 24 * HOUR),
    updated_at: iso(180 * 24 * HOUR),
    favorite: false,
    tags: ['词', '山水'],
    archived: true
  }
];

export const mockLibraryItems = BOOKS;

export const mockProjects = BOOKS.filter((b) => b.state === 'in_progress' || b.state === 'idle').map((b) => ({
  key: b.key,
  name: b.name,
  state: b.state,
  done: b.done,
  total: b.total,
  words: b.total_words,
  running: b.running,
  paused: !b.running,
  queued: b.key.includes('长夜') ? 1 : 0
}));

export const DEMO_KEY = '20261021-104233-长夜灯河';

const chapterContent = [
  '雨是后半夜落下来的。铁皮屋顶被敲得发响，像有人在上面一粒一粒地数着什么。陈砚把车停在第七码头尽头，熄了火，却没有立刻下车。',
  '挡风玻璃上浮着一层白雾。他伸手擦开一小块，看见灯塔立在雨里，光柱每十二秒扫过一次，把海面切成明暗两半。老周说，灯塔的灯不能灭，哪怕只灭一次，也会有人找不到回家的方向。',
  '可老周已经失踪十一天了。',
  '陈砚从副驾驶的帆布包里取出那份名单。纸页受潮，边角软得像一块旧布。第七个名字被水洇开了，只剩一个模糊的姓氏，像一个不肯说完的句子。',
  '“你确定要进去？”耳机里，林素问。她的声音混着电流，听上去比雨还冷。',
  '“灯亮着。”陈砚说，“说明里面有人。”',
  '他推开车门。风裹着咸腥的水汽扑过来，雨点打在脸上，竟有一点烫。远处传来一声船笛，很长，很慢，像从十几年前漂过来的。'
].join('\n\n');

function buildVersionItems() {
  return [
    { id: 'v-20261025-0930', label: '自动保存 · 第二轮校订', at: iso(5 * HOUR), words: 3140 },
    { id: 'v-20261024-2215', label: '重写前快照', at: iso(14 * HOUR), words: 2980 },
    { id: 'v-20261024-1840', label: '人工编辑', at: iso(18 * HOUR), words: 2860 },
    { id: 'v-20261023-1120', label: '初稿', at: iso(46 * HOUR), words: 2540 }
  ];
}

export const mockChapters = demoChapters;
export const mockChapterText = chapterContent;
export const mockVersions = buildVersionItems();

function buildVolumes() {
  const volumes = [];
  for (let v = 0; v < 4; v += 1) {
    const arcs = [];
    for (let a = 0; a < 3; a += 1) {
      const start = v * 30 + a * 10 + 1;
      const chapters = [];
      for (let c = start; c < start + 10; c += 1) chapters.push(c);
      arcs.push({
        no: a + 1,
        title: ARC_TITLES[v * 3 + a],
        chapters,
        goal: ['建立困局与第一份线索', '让盟友变成嫌疑人', '逼近真相并付出代价'][a]
      });
    }
    volumes.push({
      no: v + 1,
      title: VOLUME_TITLES[v],
      goal: ['确认名单的存在', '名单背后的人开始反击', '旧案与新案合流', '在灯塔完成最后的选择'][v],
      arcs
    });
  }
  return volumes;
}

export const mockPlan = {
  key: DEMO_KEY,
  plan: {
    version: 7,
    scale: '100万',
    target_words: 1000000,
    mode: '滚动规划',
    premise: '一名被停职的港城刑警，在追查守灯人失踪案时发现一份跨越二十年的失踪者名单，而名单上的每一个人都曾在那座灯塔工作过。',
    theme: '记忆如何被权力改写，以及普通人为何仍选择作证。',
    volumes: buildVolumes(),
    digest: '全书四卷，双线叙事：现在时追查失踪案，过去时回放二十年前的沉船事故。第 47 章是第二卷的转轴，名单第一次被完整读出，但真伪未定。'
  },
  proposals: [
    {
      id: 'prop-2041',
      scope: '卷二 · 第 41–60 章',
      reason: '第 46 章读者留存出现回落，需要在名单线上提前给一次可验证的小胜。',
      instruction: '压缩两章调查过程，把“第七码头对峙”提前到第 48 章。',
      created_at: iso(40 * 60 * 1000),
      status: 'pending',
      summary: '提前对峙场景；合并第 49–50 章的重复取证；将线人出场提前一章。',
      diff: '- 第49章 重复取证（约 3.1k 字）\n- 第50章 二次跟踪（约 2.8k 字）\n+ 第48章 第七码头对峙（新增 3.4k 字）\n~ 第51章 线人提前出场，动机补强'
    },
    {
      id: 'prop-2038',
      scope: '卷三 · 第 61–75 章',
      reason: '真凶身份在中段暴露过早，导致后段张力不足。',
      instruction: '把内鬼揭示推迟到第 72 章，保留两处误导线索。',
      created_at: iso(28 * HOUR),
      status: 'applied',
      summary: '内鬼揭示后移 8 章；新增两处误导；调整林素的选择动机。',
      diff: '~ 第64章 保留假证据\n~ 第72章 内鬼揭示\n+ 第69章 林素的沉默（新增 2.2k 字）'
    }
  ]
};

export const mockApproval = {
  id: 'prop-2041',
  title: '规划重排提案：提前“第七码头对峙”',
  reason: '第 46 章留存回落，需要一次可验证的小胜来抬升追读。影响卷二第 41–60 章，共 20 章。',
  scope: ['卷二', '第 41–60 章', '影响 6 章'],
  proposal_id: 'prop-2041'
};

export const mockState = {
  characters: [
    { id: 'c1', name: '陈砚', role: '主角 · 停职刑警', status: '在第七码头', first: 1, last: 47, note: '对雨声有创伤性反应' },
    { id: 'c2', name: '林素', role: '记者 · 线人', status: '失联 2 章', first: 3, last: 45, note: '掌握名单复印件' },
    { id: 'c3', name: '老周', role: '灯塔守灯人', status: '失踪', first: 1, last: 36, note: '真实身份未揭示' },
    { id: 'c4', name: '许闻', role: '港务局副处长', status: '在第 46 章出现', first: 12, last: 46, note: '疑似内鬼，动机未明' },
    { id: 'c5', name: '陈母', role: '配角', status: '住院', first: 8, last: 40, note: '记得二十年前的船名' },
    { id: 'c6', name: '阿灿', role: '码头少年', status: '逃走', first: 22, last: 44, note: '目击者' }
  ],
  locations: [
    { id: 'l1', name: '第七码头', status: '主场景', note: '雨天，铁皮屋顶' },
    { id: 'l2', name: '白塔灯塔', status: '主场景', note: '每十二秒扫过一次' },
    { id: 'l3', name: '旧港务局档案室', status: '已封', note: '第 52 章将重访' },
    { id: 'l4', name: '潮汐酒馆', status: '线人据点', note: '林素第一次出现' },
    { id: 'l5', name: '南岸废船坞', status: '未使用', note: '伏笔：沉船事故' }
  ],
  items: [
    { id: 'i1', name: '失踪者名单', status: '第 47 章已读到第七个名字', note: '受潮，字迹洇开' },
    { id: 'i2', name: '停在十一点的表', status: '陈砚保管', note: '老周的遗物？' },
    { id: 'i3', name: '潮汐记录本', status: '缺 1998 年 7 月', note: '第 54 章补全' },
    { id: 'i4', name: '半枚指纹', status: '未比对', note: '指向许闻' }
  ],
  threads: [
    { id: 't1', name: '守灯人失踪案', status: '进行中', progress: 62, note: '主线' },
    { id: 't2', name: '二十年前的沉船', status: '进行中', progress: 34, note: '暗线' },
    { id: 't3', name: '林素的身份', status: '待揭示', progress: 18, note: '第 58 章' },
    { id: 't4', name: '许闻与港务局', status: '进行中', progress: 45, note: '卷二收束' },
    { id: 't5', name: '陈母的记忆', status: '搁置', progress: 12, note: '卷四回收' }
  ],
  foreshadowing: [
    { id: 'f1', text: '老周说“灯灭一次，就会有人找不到家”', due: 47, status: 'due', note: '本章兑现' },
    { id: 'f2', text: '停在十一点的表', due: 52, status: 'open', note: '第 52 章解释' },
    { id: 'f3', text: '1998 年 7 月的潮汐记录', due: 54, status: 'open', note: '缺页原因' },
    { id: 'f4', text: '阿灿提到的“穿制服的人”', due: 45, status: 'overdue', note: '已逾期 2 章' },
    { id: 'f5', text: '林素说“我只信你一次”', due: 60, status: 'open', note: '情感线伏笔' }
  ],
  issues: [
    { id: 's1', severity: 'high', text: '第 44 章阿灿说“只见过一次”，第 46 章又说“每周都去码头”，知情范围冲突。', ref: '第 44 / 46 章' },
    { id: 's2', severity: 'medium', text: '许闻在第 12 章称不认识老周，但第 45 章回忆里二人同框，需补一句解释。', ref: '第 12 / 45 章' },
    { id: 's3', severity: 'low', text: '雨势在第 43–46 章反复由“小雨”变“暴雨”，建议统一为渐强。', ref: '第 43–46 章' }
  ],
  timeline: [
    { at: 'D-20', text: '老周最后一次出现在灯塔', current: false },
    { at: 'D-11', text: '守灯人失踪案立案', current: false },
    { at: 'D-6', text: '林素在潮汐酒馆递给陈砚第一张照片', current: false },
    { at: 'D-2', text: '陈砚被停职，交出配枪', current: false },
    { at: '今夜 02:40', text: '陈砚抵达第七码头，雨开始下大', current: true },
    { at: '今夜 03:00', text: '计划：进入灯塔，读到名单第七个名字', current: false },
    { at: 'D+1', text: '许闻约谈陈砚（未写）', current: false }
  ],
  metrics: { chapters: 120, written: 46, words: 146800, avg_score: 88.5, open_threads: 4, due_foreshadowing: 1 }
};

export const mockReferences = [
  {
    id: 'ref-1',
    title: '《港城旧事》地方志摘录',
    kind: 'text',
    source: '粘贴文本',
    chars: 18400,
    usage_mode: 'facts',
    created_at: iso(6 * 24 * HOUR),
    summary: '记录了 1998 年南岸沉船事故的公开报道口径，与小说中的官方叙事形成偏差。',
    tags: ['地方志', '1998'],
    copyright_confirmed: true
  },
  {
    id: 'ref-2',
    title: '冷硬派悬疑风格样本',
    kind: 'text',
    source: '粘贴文本',
    chars: 9600,
    usage_mode: 'style',
    created_at: iso(6 * 24 * HOUR),
    summary: '短句、低温叙述、以物写人；避免形容词堆叠与总结式抒情。',
    tags: ['风格', '冷硬'],
    copyright_confirmed: true
  },
  {
    id: 'ref-3',
    title: '港区夜班船员访谈录音整理',
    kind: 'file',
    source: '访谈-船员.docx',
    chars: 7300,
    usage_mode: 'adapt',
    created_at: iso(4 * 24 * HOUR),
    summary: '提供了码头的真实工作节奏、俚语与夜间光线细节。',
    tags: ['访谈', '细节'],
    copyright_confirmed: true
  },
  {
    id: 'ref-4',
    title: '灯塔结构与值守制度（百科）',
    kind: 'url',
    source: 'https://example.org/lighthouse',
    chars: 4200,
    usage_mode: 'structure',
    created_at: iso(3 * 24 * HOUR),
    summary: '灯塔值守轮班、灯器维护周期，可用于第 47 章的技术细节。',
    tags: ['结构', '设定'],
    copyright_confirmed: false
  }
];

export const mockSkills = {
  candidates: [
    {
      id: 'cand-1',
      name: '悬念钩子 · 章末强化',
      target: 'chapter_hook',
      delta: '+6.4',
      reason: '近 20 章章末钩子评分低于全书均值，候选规则将“未完成动作”前置。',
      pending: true,
      diff: '- 章末：收束情绪，给一句总结。\n+ 章末：留下一个未完成动作或未回答问题。\n+ 禁止用“他知道，一切才刚刚开始”这类套话收尾。'
    },
    {
      id: 'cand-2',
      name: '连续性 · 知情范围校验',
      target: 'continuity',
      delta: '+4.1',
      reason: '第 44–46 章连续出现人物知情范围冲突。',
      pending: false,
      diff: '+ 生成前先列出本章每个角色的“已知信息 / 未知信息”。\n+ 对话中不得出现角色尚未获知的事实。'
    },
    {
      id: 'cand-3',
      name: '去 AI 腔 · 感官替代',
      target: 'style',
      delta: '+2.8',
      reason: '“仿佛 / 瞬间 / 不由得”密度仍高于阈值。',
      pending: false,
      diff: '- 禁用“仿佛、瞬间、不由得”作为默认连接词。\n+ 用具体动作、温度、声音替代抽象感受。'
    }
  ],
  versions: [
    { name: 'chapter_hook', version: 'v4', active: true, at: iso(4 * 24 * HOUR), note: '章末钩子候选转正' },
    { name: 'continuity', version: 'v7', active: true, at: iso(11 * 24 * HOUR), note: '增加知情范围校验' },
    { name: 'style', version: 'v3', active: false, at: iso(20 * 24 * HOUR), note: '被 v4 替代' },
    { name: 'chapter_hook', version: 'v3', active: false, at: iso(30 * 24 * HOUR), note: '回滚点' }
  ],
  feedback: { total: 38, avg_rating: 4.3, tags: [{ tag: '钩子', count: 12 }, { tag: '人物一致', count: 9 }, { tag: '语言', count: 7 }] }
};

export const mockLearn = {
  total: 14,
  avg_quality: 87.6,
  insight: '你已写 14 本，最常写「悬疑」（5 本），历史平均成书质量 87.6 分。系统已按你的偏好预填开书选项。',
  genres: [
    { genre: '悬疑', count: 5, share: 36, avg_quality: 89.2, top_hand: '信息差', top_pace: '中速', top_lang: '冷硬' },
    { genre: '古言', count: 3, share: 21, avg_quality: 88.1, top_hand: '双强', top_pace: '慢热', top_lang: '雅正' },
    { genre: '剧本', count: 2, share: 14, avg_quality: 90.4, top_hand: '密闭空间', top_pace: '紧凑', top_lang: '口语' },
    { genre: '散文', count: 2, share: 14, avg_quality: 90.6, top_hand: '细节', top_pace: '舒缓', top_lang: '克制' },
    { genre: '玄幻', count: 2, share: 14, avg_quality: 79.8, top_hand: '升级', top_pace: '快', top_lang: '爽感' }
  ]
};

export const mockStyles = [
  { name: '冷硬港城', genre: '悬疑', summary: '短句、低温、以物写人；对白克制，留白多。', created_at: iso(6 * 24 * HOUR), fingerprint_words: ['雨', '铁皮', '潮汐'] },
  { name: '古典雅正', genre: '古言', summary: '书面语与器物细节并重，情绪藏在礼数之下。', created_at: iso(30 * 24 * HOUR), fingerprint_words: ['灯', '檐', '旧年'] },
  { name: '市井烟火', genre: '都市', summary: '口语鲜活，场景有声有味，人物先于情节。', created_at: iso(44 * 24 * HOUR), fingerprint_words: ['蒜', '油烟', '楼道'] },
  { name: '克制散文', genre: '散文', summary: '少形容词，多名词与动作；结尾不点题。', created_at: iso(12 * 24 * HOUR), fingerprint_words: ['母亲', '窗', '雪'] }
];

export const mockMessages = [
  {
    id: 'msg-1',
    role: 'user',
    content: '第 47 章我想让主角在旧灯塔里发现第一份名单，但先不要揭露守灯人的真实身份。',
    created_at: iso(52 * 60 * 1000),
    attachments: [{ id: 'ref-1', title: '《港城旧事》地方志摘录', usage_mode: 'facts' }]
  },
  {
    id: 'msg-2',
    role: 'assistant',
    content:
      '第 47 章的大纲已经按“**只给名单、不给答案**”重排。三条线索的落点如下：\n\n### 本章落点\n\n1. **名单出现**：陈砚在灯塔值守室的地板夹层里找到受潮的名单，读到第七个名字。\n2. **身份悬置**：老周的身份只通过物件暗示（停在十一点的表、值班表上的涂改），不给正面确认。\n3. **外部压力**：许闻的电话在名单出现后立即打进来，制造“被看见”的寒意。\n\n> 雨声要更冷：删掉所有“雨像……”的比喻，只写雨落在什么材质上、发出什么声音。\n\n本轮会保留第 46 章结尾的 `船笛` 意象，并把它作为本章最后一个声音。',
    created_at: iso(51 * 60 * 1000),
    process: {
      title: '检索记忆与规划',
      stage: '写作 · 第 47 章',
      model: 'deepseek-chat',
      duration: '18.6s',
      tokens: 4820,
      retries: 1,
      steps: [
        { label: '检索作品记忆（人物 / 地点 / 伏笔）', state: 'done', ms: '2.1s' },
        { label: '读取第 41–46 章摘要与章末钩子', state: 'done', ms: '1.4s' },
        { label: '生成章节目标与场景节拍', state: 'done', ms: '4.8s' },
        { label: '自检：知情范围 / 伏笔到期', state: 'done', ms: '1.2s' },
        { label: '输出大纲', state: 'done', ms: '9.1s' }
      ]
    },
    rating: null
  },
  {
    id: 'msg-3',
    role: 'user',
    content: '好，按这个方案继续。把开头的雨声写得更冷一些。',
    created_at: iso(46 * 60 * 1000)
  },
  {
    id: 'msg-4',
    role: 'assistant',
    content:
      '好的，我先把第 47 章的开场写到 800 字左右，再交给你确认语气。\n\n先说两个我会主动守住的东西：\n\n- **不解释老周**：本章只出现他的物件与别人的转述。\n- **不让陈砚推理过度**：他只在最后做一个很小的判断，而且可能是错的。\n\n如果你希望开场直接从“读到第七个名字”切入，我也可以把雨声压成背景音，把悬念提前到第一段。',
    created_at: iso(45 * 60 * 1000),
    process: {
      title: '写作准备',
      stage: '正文 · 第 47 章',
      model: 'deepseek-chat',
      duration: '3.2s',
      tokens: 1180,
      retries: 0,
      steps: [
        { label: '读取本章目标', state: 'done', ms: '0.4s' },
        { label: '对齐文风与禁用词', state: 'done', ms: '0.6s' },
        { label: '生成开场方案', state: 'done', ms: '2.2s' }
      ]
    },
    rating: null
  }
];

export const mockChatReplies = [
  '收到。我会按“**只推进一小步**”的方式处理这一轮：\n\n1. 先确认这一章的目标读者情绪（寒意 / 紧迫 / 心疼）。\n2. 只写一个场景，不跨章收束。\n3. 章末留下一个未完成的动作。\n\n如果你没有别的补充，我就从灯塔值守室的门开始写。',
  '这一轮我建议把动作拆细：\n\n- 进门之前：只写手、雨、灯。\n- 进门之后：先写气味，再写声音。\n- 名单出现：不要立刻读全，只读第七个名字。\n\n这样第 48 章还有东西可给。',
  '可以。我会保留 `船笛` 作为收尾声音，并把许闻的电话放在名单出现后的第三段，制造“被看见”的错觉。'
];

export const mockSettings = {
  quality_mode: 'report',
  token_budget: 800000,
  lorebook: true,
  llm_trace: true,
  call_gap: 2,
  max_global_agents: 4,
  pass_line: 85,
  skill_evolution: true
};

export const mockProviders = {
  default_provider: 'prov-deepseek',
  providers: [
    {
      id: 'prov-deepseek',
      name: 'DeepSeek',
      base_url: 'https://api.deepseek.com/v1',
      models: ['deepseek-chat', 'deepseek-reasoner'],
      default_model: 'deepseek-chat',
      active: true,
      local: false,
      has_key: true,
      key_display: 'sk-****'
    },
    {
      id: 'prov-agnes',
      name: 'Agnes AI',
      base_url: 'https://api.agnes-ai.cn/v1',
      models: ['agnes-3.0-flash', 'agnes-2.5-flash'],
      default_model: 'agnes-3.0-flash',
      active: true,
      local: false,
      has_key: true,
      key_display: 'sk-****'
    },
    {
      id: 'prov-ollama',
      name: 'Ollama 本地',
      base_url: 'http://127.0.0.1:11434/v1',
      models: ['qwen2.5:14b'],
      default_model: 'qwen2.5:14b',
      active: false,
      local: true,
      has_key: false,
      key_display: ''
    }
  ],
  presets: []
};

export const mockModels = {
  default_provider: 'prov-deepseek',
  options: [
    { id: 'prov-deepseek', name: 'DeepSeek', models: ['deepseek-chat', 'deepseek-reasoner'], default_model: 'deepseek-chat' },
    { id: 'prov-agnes', name: 'Agnes AI', models: ['agnes-3.0-flash', 'agnes-2.5-flash'], default_model: 'agnes-3.0-flash' }
  ]
};

export const mockMediums = [
  {
    key: 'novel',
    label: '小说',
    icon: 'book',
    desc: '长篇连载、中篇与短篇集，支持大纲优先与滚动规划。',
    dir_prefix: '',
    pass_line: 85,
    stages: [
      { code: 'premise', name: '立意', desc: '题材、命题与读者承诺' },
      { code: 'plan', name: '规划', desc: '人物、卷弧章与伏笔' },
      { code: 'draft', name: '写作', desc: '逐章生成并通过字数门禁' },
      { code: 'review', name: '审稿', desc: '六维评分与定向重写' }
    ],
    score_dims: [
      { name: '结构', weight: 20 },
      { name: '因果可信', weight: 15 },
      { name: '人物', weight: 20 },
      { name: '连续性', weight: 15 },
      { name: '语言', weight: 15 },
      { name: '读者体验', weight: 15 }
    ]
  },
  {
    key: 'script',
    label: '剧本',
    icon: 'film',
    desc: '长片、剧集、短片、舞台与戏曲，按场景与节拍组织。',
    dir_prefix: 's_',
    pass_line: 85,
    stages: [
      { code: 'premise', name: '戏核', desc: '一句话前提与类型' },
      { code: 'outline', name: '分场', desc: '幕、场与节拍表' },
      { code: 'draft', name: '场景', desc: '逐场对白与动作' },
      { code: 'polish', name: '打磨', desc: '潜台词与节奏' }
    ],
    score_dims: [
      { name: '结构', weight: 25 },
      { name: '冲突', weight: 20 },
      { name: '人物', weight: 20 },
      { name: '对白', weight: 20 },
      { name: '可拍性', weight: 15 }
    ]
  },
  {
    key: 'poetry',
    label: '诗词',
    icon: 'feather',
    desc: '格律诗、词牌、古风与现代诗，内置平水韵校验。',
    dir_prefix: 'p_',
    pass_line: 80,
    stages: [
      { code: 'premise', name: '立意', desc: '情景与寄托' },
      { code: 'draft', name: '成篇', desc: '按体式起承转合' },
      { code: 'check', name: '校验', desc: '格律、韵部与重字' }
    ],
    score_dims: [
      { name: '格律', weight: 30 },
      { name: '意象', weight: 25 },
      { name: '语言', weight: 25 },
      { name: '立意', weight: 20 }
    ]
  },
  {
    key: 'prose',
    label: '散文',
    icon: 'pen',
    desc: '抒情、叙事、哲理与游记，强调细节与反 AI 腔。',
    dir_prefix: 'e_',
    pass_line: 85,
    stages: [
      { code: 'premise', name: '取材', desc: '经验、观察与切口' },
      { code: 'draft', name: '成文', desc: '细节推进，不点题' },
      { code: 'scan', name: '去腔', desc: '扫描套话与抽象表达' }
    ],
    score_dims: [
      { name: '细节', weight: 30 },
      { name: '语言', weight: 25 },
      { name: '结构', weight: 20 },
      { name: '真诚', weight: 25 }
    ]
  }
];

export const mockUsage = {
  today_tokens: 92400,
  month_tokens: 1840000,
  budget: 800000,
  calls: 138,
  by_task: [
    { task: '章节写作', tokens: 1260000, share: 68 },
    { task: '规划与重规划', tokens: 312000, share: 17 },
    { task: '审稿', tokens: 198000, share: 11 },
    { task: '对话', tokens: 70000, share: 4 }
  ]
};

export const mockSearchHits = [
  { ref_id: 'ref-1', title: '《港城旧事》地方志摘录', chunk_no: 12, snippet: '……1998 年 7 月 14 日夜，南岸航道因暴雨临时封闭，渡轮“白鹭号”偏离航线……', score: 0.91 },
  { ref_id: 'ref-3', title: '港区夜班船员访谈录音整理', chunk_no: 4, snippet: '……灯塔的灯每十二秒转一次，转的时候整间值守室会亮一下，像有人从窗外看你……', score: 0.84 },
  { ref_id: 'ref-4', title: '灯塔结构与值守制度（百科）', chunk_no: 2, snippet: '……值守人员每四小时记录一次潮汐与能见度，记录本按年份装订……', score: 0.72 }
];

export const mockCandidateRef = {
  id: 'ref-new',
  title: '新上传的参考资料',
  kind: 'file',
  source: '上传文件',
  chars: 5200,
  usage_mode: 'style',
  created_at: iso(0),
  summary: '等待分析：上传后可在列表中点击「分析」生成摘要与可用片段。',
  tags: [],
  copyright_confirmed: true
};

export function mockAssistantReplyFor(text) {
  const t = String(text || '');
  if (/名单|灯塔|第\s*47/.test(t)) return mockChatReplies[0];
  if (/节奏|拆|场景/.test(t)) return mockChatReplies[1];
  if (/许闻|电话|船笛/.test(t)) return mockChatReplies[2];
  return '收到。我会先按当前作品的规划与文风约束处理这一轮，不跨章收束，也不改动已经通过审稿的章节。\n\n需要我同时更新右侧的**状态**与**规划**吗？';
}
