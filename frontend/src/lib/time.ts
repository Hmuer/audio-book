/**
 * 阿布平台通用时间工具。
 *
 * 后端 datetime 以 UTC 存储，JSON 序列化时不带时区后缀（形如 `2026-08-29T12:34:56`）。
 * 原生 `new Date(iso)` 在无显式时区后缀时会按「本地时区」解析，Asia/Shanghai 正好差 +8h，
 * 导致刚创建的记录显示成"8 小时前"；排序也会整体偏移一天。
 *
 * 统一入口 parseTime：无时区后缀一律追加 Z 强制按 UTC 解析；已有后缀（Z / +08:00 / -05:30）则直接交给浏览器。
 */

export function parseTime(iso: string | null | undefined): Date {
  if (!iso) return new Date(NaN);
  const hasTz = /Z|[+-]\d{2}:?\d{2}$/.test(iso);
  if (hasTz) return new Date(iso);
  const normalized = iso.replace(' ', 'T');
  return new Date(normalized + 'Z');
}

export function formatDate(iso: string | null | undefined, locale = 'zh-CN'): string {
  const d = parseTime(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString(locale);
}

export function formatDateTime(iso: string | null | undefined, locale = 'zh-CN'): string {
  const d = parseTime(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString(locale);
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    const t = parseTime(iso).getTime();
    if (Number.isNaN(t)) return iso;
    const diff = Date.now() - t;
    if (diff < 60 * 1000) return '刚刚';
    if (diff < 60 * 60 * 1000) return `${Math.floor(diff / 60000)} 分钟前`;
    if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / 3600000)} 小时前`;
    if (diff < 7 * 24 * 60 * 60 * 1000) return `${Math.floor(diff / 86400000)} 天前`;
    return formatDate(iso);
  } catch {
    return iso;
  }
}

/** 把两组 ISO 时间戳按 UTC 毫秒差做升序/降序排序 */
export function sortByTimeDesc<T>(list: T[], getIso: (item: T) => string | null | undefined): T[] {
  return [...list].sort((a, b) => {
    const ta = parseTime(getIso(a)).getTime();
    const tb = parseTime(getIso(b)).getTime();
    return tb - ta;
  });
}
