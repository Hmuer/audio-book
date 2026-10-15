export type BuildControlAction = 'pause' | 'resume' | 'stop';

export function isBuildRunning(status: string): boolean {
  return ['queued', 'running', 'pausing', 'stopping'].includes(status);
}

export function isBuildBlocking(status: string): boolean {
  return isBuildRunning(status) || status === 'paused';
}

export function canControlBuild(status: string, action: BuildControlAction): boolean {
  if (action === 'pause') return status === 'queued' || status === 'running';
  if (action === 'resume') return status === 'paused';
  return ['queued', 'running', 'pausing', 'paused'].includes(status);
}

export function buildControlMessage(status: string): string | null {
  if (status === 'pausing') return '暂停中：等待当前章节与已派发 TTS，或当前 ZIP 分片完成后暂停。';
  if (status === 'paused') return '已暂停，不会自动继续。可查看和下载已有章节；请继续或停止现有构建后再开始生成。';
  if (status === 'stopping') return '停止中：等待当前章节与已派发 TTS，或当前 ZIP 分片完成后停止。';
  if (status === 'queued' || status === 'running') return '暂停或停止将在安全边界生效，当前章节与已派发 TTS，或当前 ZIP 分片会完成；不能立即中断供应商已接受的请求，在途请求可能继续计费。';
  return null;
}

export const BUILD_STOP_CONFIRMATION = '确认停止该构建？停止后不能继续原构建；已完成章节、产物和段缓存会保留。当前章节与已派发 TTS，或当前 ZIP 分片完成后才会停止，供应商已接受的请求可能继续计费。';
