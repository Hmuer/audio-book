// 音色数据工具：多厂商（MiniMax / 豆包 / ICL 复刻）字段归一化
// 后端各 provider 字段口径不一致：
//   minimax: gender="男声"/"女声"/"中性"，description 有值
//   doubao : gender="male"/"female"/"neutral"，无 description，有 zh_tags/scene/age/dialect
//   icl    : gender="neutral"，有 zh_tags

import { Voice } from './api';

export type GenderKey = '男声' | '女声' | '中性';

const GENDER_MAP: Record<string, GenderKey> = {
  male: '男声',
  female: '女声',
  neutral: '中性',
  男: '男声',
  男声: '男声',
  女: '女声',
  女声: '女声',
  中性: '中性',
};

export function normalizeGender(v: Voice): GenderKey {
  return GENDER_MAP[v.gender] || '中性';
}

/** 展示用描述：minimax 用 description，doubao/icl 用中文标签/场景兜底 */
export function voiceDescription(v: Voice): string {
  if (v.description) return v.description;
  if (v.zh_tags?.length) return v.zh_tags.join(' · ');
  if (v.scene?.length) return v.scene.join(' / ');
  return '—';
}

export type ProviderKey = 'minimax' | 'doubao' | 'icl';

export function voiceProvider(v: Voice): ProviderKey {
  if (v.provider === 'minimax' || v.id.startsWith('minimax:')) return 'minimax';
  if (v.provider === 'icl' || v.id.startsWith('icl:')) return 'icl';
  return 'doubao';
}

export const PROVIDER_LABELS: Record<ProviderKey, string> = {
  minimax: 'MiniMax',
  doubao: '豆包',
  icl: '我的复刻',
};

/** 场景筛选选项：聚合当前音色集合的所有 scene 值（去重排序） */
export function sceneOptions(voices: Voice[]): string[] {
  const s = new Set<string>();
  voices.forEach(v => (v.scene || []).forEach(x => x && s.add(x)));
  return Array.from(s).sort((a, b) => a.localeCompare(b, 'zh'));
}

/** 年龄筛选选项 + 中文标签 */
export const AGE_LABELS: Record<string, string> = {
  child: '儿童',
  teen: '少年',
  youth: '青年',
  middle: '中年',
  old: '老年',
};

/** 方言 key → 中文显示 */
export const DIALECT_LABELS: Record<string, string> = {
  sichuan: '四川话',
  cantonese: '粤语',
  dongbei: '东北话',
  shaanxi: '陕西话',
  shanghai: '上海话',
  minnan: '闽南语',
  changsha: '长沙话',
  hefei: '合肥话',
  tianjin: '天津话',
  shandong: '山东话',
  henan: '河南话',
  xinjiang: '新疆话',
};

export function dialectLabel(d: string | undefined | null): string {
  if (!d) return '';
  return DIALECT_LABELS[d] || d;
}

export function ageOptions(voices: Voice[]): string[] {
  const s = new Set<string>();
  voices.forEach(v => v.age && s.add(v.age));
  return Array.from(s).filter(a => AGE_LABELS[a]);
}

/** 搜索 + 多维筛选（厂商/性别/场景/年龄/方言）。维度为空或 'all' 表示不过滤。 */
export function filterVoices(
  voices: Voice[],
  opts: {
    q?: string;
    provider?: ProviderKey | 'all';
    gender?: GenderKey | 'all';
    scene?: string;
    age?: string;
    dialectOnly?: boolean;
  }
): Voice[] {
  const q = (opts.q || '').trim().toLowerCase();
  return voices.filter(v => {
    if (opts.provider && opts.provider !== 'all' && voiceProvider(v) !== opts.provider)
      return false;
    if (opts.gender && opts.gender !== 'all' && normalizeGender(v) !== opts.gender)
      return false;
    if (opts.scene && !(v.scene || []).includes(opts.scene)) return false;
    if (opts.age && v.age !== opts.age) return false;
    if (opts.dialectOnly && !v.dialect) return false;
    if (!q) return true;
    const hay = [
      v.name,
      v.id,
      voiceDescription(v),
      ...(v.zh_tags || []),
      ...(v.scene || []),
      v.dialect || '',
    ]
      .join('\n')
      .toLowerCase();
    return hay.includes(q);
  });
}
