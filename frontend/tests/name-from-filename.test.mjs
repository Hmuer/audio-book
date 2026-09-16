/**
 * nameFromFilename 的纯函数测试。
 * 实现位于 src/components/CreateAudiobookDialog.tsx（TS 函数，无法直接 import），
 * 这里把核心逻辑复制一份做 RED→GREEN 验证；两份实现都遵守同样的合约：
 *
 *   nameFromFilename(filename) = filename 去掉最后一个 .ext
 *     "神秘峡谷.txt"   → "神秘峡谷"
 *     "alice.epub"     → "alice"
 *     "book.tar.gz"    → "book.tar"
 *     "no-extension"   → "no-extension"
 *     ".env"           → ".env"
 *     ""               → ""
 *
 * 运行：node frontend/tests/name-from-filename.test.mjs
 */
import { deepStrictEqual } from 'node:assert';
import { test } from 'node:test';

// Mirror of the implementation in CreateAudiobookDialog.tsx
function nameFromFilename(filename) {
  if (!filename) return '';
  const i = filename.lastIndexOf('.');
  if (i <= 0) return filename;
  return filename.slice(0, i);
}

test('nameFromFilename: 典型小说文件名去后缀', () => {
  deepStrictEqual(nameFromFilename('神秘峡谷.txt'), '神秘峡谷');
  deepStrictEqual(nameFromFilename('alice.epub'), 'alice');
  deepStrictEqual(nameFromFilename('README.md'), 'README');
});

test('nameFromFilename: 多层后缀只剥最后一层', () => {
  deepStrictEqual(nameFromFilename('book.tar.gz'), 'book.tar');
  deepStrictEqual(nameFromFilename('a.b.c.d'), 'a.b.c');
});

test('nameFromFilename: 无后缀原样保留', () => {
  deepStrictEqual(nameFromFilename('no-extension'), 'no-extension');
  deepStrictEqual(nameFromFilename('神秘峡谷'), '神秘峡谷');
});

test('nameFromFilename: 隐藏文件保留整段', () => {
  // ".env" 是隐藏文件，没有"主名"，整段保留
  deepStrictEqual(nameFromFilename('.env'), '.env');
  deepStrictEqual(nameFromFilename('.bashrc'), '.bashrc');
});

test('nameFromFilename: 边界值', () => {
  deepStrictEqual(nameFromFilename(''), '');
  // 文件名只有一个 "." 也不是合法隐藏文件（i=0），返回原样
  deepStrictEqual(nameFromFilename('.'), '.');
  // 单字符 "x.txt" → "x"
  deepStrictEqual(nameFromFilename('x.txt'), 'x');
});
