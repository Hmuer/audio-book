'use client';

import { useEffect, useState } from 'react';
import { api, ProjectListItem } from '@/lib/api';

// 状态徽章：颜色映射，颜色规范按任务要求
// draft=灰, imported=蓝, preparing=黄, ready=青, synthesizing=橙(pulse), done=绿, failed=红
const STATUS_STYLES: Record<string, string> = {
  draft: 'bg-gray-500/20 text-gray-300 border-gray-500/30',
  imported: 'bg-blue-500/20 text-blue-300 border-blue-500/30',
  preparing: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
  ready: 'bg-cyan-500/20 text-cyan-300 border-cyan-500/30',
  synthesizing: 'bg-orange-500/20 text-orange-300 border-orange-500/30 animate-pulse',
  done: 'bg-green-500/20 text-green-300 border-green-500/30',
  failed: 'bg-red-500/20 text-red-300 border-red-500/30',
};

const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  imported: '已导入',
  preparing: '识别中',
  ready: '就绪',
  synthesizing: '合成中',
  done: '已完成',
  failed: '失败',
};

// 状态徽章组件
export function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_STYLES[status] || 'bg-white/10 text-white/70 border-white/20';
  const label = STATUS_LABEL[status] || status;
  return (
    <span className={`chip border ${cls}`}>
      {label}
    </span>
  );
}

// 格式化时间："3 分钟前"、"2 小时前"、"昨天" 等相对时间
function relativeTime(iso: string): string {
  try {
    const t = new Date(iso).getTime();
    const diff = Date.now() - t;
    if (diff < 60 * 1000) return '刚刚';
    if (diff < 60 * 60 * 1000) return `${Math.floor(diff / 60000)} 分钟前`;
    if (diff < 24 * 60 * 60 * 1000) return `${Math.floor(diff / 3600000)} 小时前`;
    if (diff < 7 * 24 * 60 * 60 * 1000) return `${Math.floor(diff / 86400000)} 天前`;
    return new Date(iso).toLocaleDateString('zh-CN');
  } catch {
    return iso;
  }
}

export default function ProjectListPage() {
  const [items, setItems] = useState<ProjectListItem[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // 删除确认状态：保存当前 hover 项目 id 和待确认删除的 id
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  // 拉取项目列表
  const reload = async () => {
    setErr(null);
    try {
      const list = await api.projectList();
      setItems(list);
    } catch (e: any) {
      setErr(String(e?.message || e));
      setItems([]);
    }
  };

  useEffect(() => {
    reload();
  }, []);

  // 删除项目
  const onDelete = async (id: string) => {
    try {
      await api.projectDelete(id);
      setConfirmDeleteId(null);
      await reload();
    } catch (e: any) {
      setErr(`删除失败: ${e?.message || e}`);
      setConfirmDeleteId(null);
    }
  };

  const loading = items === null;

  return (
    <section className="space-y-6">
      {/* 顶部操作栏 */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div>
          <h2 className="text-lg font-semibold">📚 项目工作台</h2>
          <p className="text-sm text-white/60 mt-1">
            管理你的整本有声书项目 · 每个项目独立保存章节、角色音色与构建历史
          </p>
        </div>
        <button
          className="btn-primary"
          onClick={() => {
            window.location.hash = '#/projects/new';
          }}
        >
          ＋ 新建项目
        </button>
      </div>

      {err && (
        <div className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          {err}
        </div>
      )}

      {/* 加载中 */}
      {loading && (
        <div className="card text-center py-12 text-white/60">
          <div className="inline-block w-8 h-8 border-4 border-brand-500 border-t-transparent rounded-full animate-spin mb-3" />
          <div>加载项目列表…</div>
        </div>
      )}

      {/* 空状态 */}
      {!loading && items && items.length === 0 && (
        <div className="card text-center py-16 max-w-2xl mx-auto">
          <div className="text-6xl mb-4">📭</div>
          <h3 className="text-lg font-semibold mb-2">还没有项目</h3>
          <p className="text-sm text-white/60 mb-6">
            创建你的第一个有声书项目，上传 TXT 后系统会自动识别章节与角色，
            然后配置音色即可一键生成多章节 MP3。
          </p>
          <button
            className="btn-primary"
            onClick={() => {
              window.location.hash = '#/projects/new';
            }}
          >
            ＋ 创建第一个项目
          </button>
        </div>
      )}

      {/* 卡片网格 */}
      {!loading && items && items.length > 0 && (
        <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {items.map(p => (
            <div
              key={p.project_id}
              className="group card relative overflow-hidden cursor-pointer hover:border-brand-500/50 hover:bg-brand-500/5 transition"
              onClick={() => {
                window.location.hash = `#/projects/${p.project_id}`;
              }}
            >
              {/* 左侧色条 */}
              <div
                className="absolute left-0 top-0 bottom-0 w-1.5"
                style={{ backgroundColor: p.cover_color || '#a855f7' }}
              />

              <div className="pl-2 space-y-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="font-semibold truncate">
                      {p.book_title || p.name}
                    </div>
                    {p.book_title && p.book_title !== p.name && (
                      <div className="text-xs text-white/40 truncate mt-0.5">
                        {p.name}
                      </div>
                    )}
                  </div>
                  <StatusBadge status={p.status} />
                </div>

                <div className="flex items-center gap-3 text-xs text-white/60">
                  <span title="章节数">📜 {p.chapter_count} 章</span>
                  <span title="源文件">
                    {p.source_filename ? `📄 ${p.source_filename}` : '📄 未上传'}
                  </span>
                </div>

                <div className="flex items-center justify-between text-xs text-white/40">
                  <span>更新于 {relativeTime(p.updated_at)}</span>
                </div>
              </div>

              {/* hover 显示删除按钮 */}
              <button
                className="absolute top-2 right-2 opacity-0 group-hover:opacity-100 transition chip bg-red-500/20 text-red-300 hover:bg-red-500/30 border border-red-500/30"
                onClick={e => {
                  e.stopPropagation();
                  setConfirmDeleteId(p.project_id);
                }}
                title="删除项目"
              >
                🗑 删除
              </button>

              {/* 删除确认弹层 */}
              {confirmDeleteId === p.project_id && (
                <div
                  className="absolute inset-0 bg-gray-950/95 backdrop-blur-sm flex flex-col items-center justify-center text-center p-4 z-10"
                  onClick={e => e.stopPropagation()}
                >
                  <div className="text-2xl mb-2">⚠️</div>
                  <div className="font-semibold mb-1">确认删除该项目？</div>
                  <div className="text-xs text-white/60 mb-4">
                    删除后无法恢复，所有章节、音色配置与构建历史都会丢失。
                  </div>
                  <div className="flex gap-2">
                    <button
                      className="btn-ghost"
                      onClick={e => {
                        e.stopPropagation();
                        setConfirmDeleteId(null);
                      }}
                    >
                      取消
                    </button>
                    <button
                      className="btn bg-red-600 hover:bg-red-500 text-white"
                      onClick={e => {
                        e.stopPropagation();
                        onDelete(p.project_id);
                      }}
                    >
                      🗑 确认删除
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
