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

test('nameFromFilename: 大小写后缀（只剥，不转换）', () => {
  // 不做 lowercase / case-insensitive 处理 —— 用户命名时大小写是有意义的
  deepStrictEqual(nameFromFilename('Alice.TXT'), 'Alice');
  deepStrictEqual(nameFromFilename('book.EPUB'), 'book');
});

test('nameFromFilename: 数字文件名', () => {
  deepStrictEqual(nameFromFilename('123.txt'), '123');
  deepStrictEqual(nameFromFilename('v1.0.zip'), 'v1.0');
});

test('nameFromFilename: 包含特殊字符（中文、括号、连字符）', () => {
  deepStrictEqual(nameFromFilename('我的书（第2卷）.txt'), '我的书（第2卷）');
  deepStrictEqual(nameFromFilename('book - copy (1).epub'), 'book - copy (1)');
});

test('nameFromFilename: 文件名尾部有点/空格', () => {
  // 末尾的 "." 仍然算 ext 分隔点，剥掉
  deepStrictEqual(nameFromFilename('foo.'), 'foo');
  // 末尾空格保留
  deepStrictEqual(nameFromFilename('foo .txt'), 'foo ');
});

// ============================================================
// pickFile 行为的端到端模拟
// ============================================================
// CreateAudiobookDialog 里 pickFile 的合约：
//   1. setFile(f)
//   2. if (f && (nameAutoFilled || name 为空)) {
//        nameAutoFilled = true;
//        setName(nameFromFilename(f.name))
//      }
// 3. 用户在 name 输入框输入时：nameAutoFilled = false
// 用一个迷你 store 模拟 React state，验证以下合约：
//   - 用户没填名字：预填
//   - 用户已经手敲过名字：不覆盖
//   - 自动预填过、选另一个文件：名字跟着更新（之前那个 bug）
//   - 取消选择（file=null）：不清空名字
// ============================================================

function makePicker() {
  let name = '';
  let file = null;
  let nameAutoFilled = false;
  const pickFile = (f) => {
    file = f;
    if (f && (nameAutoFilled || !name.trim())) {
      nameAutoFilled = true;
      name = nameFromFilename(f.name);
    }
  };
  const userTypeName = (s) => {
    nameAutoFilled = false;
    name = s;
  };
  return { pickFile, userTypeName, getName: () => name, getFile: () => file };
}

test('pickFile: 用户没填名字时，名字预填为文件名去后缀', () => {
  const p = makePicker();
  p.pickFile({ name: '神秘峡谷.epub' });
  deepStrictEqual(p.getName(), '神秘峡谷');
  deepStrictEqual(p.getFile().name, '神秘峡谷.epub');
});

test('pickFile: 用户已经手动输入过名字时，再选文件不覆盖', () => {
  const p = makePicker();
  p.userTypeName('我自定义的项目名');
  p.pickFile({ name: 'alice.epub' });
  deepStrictEqual(p.getName(), '我自定义的项目名');
});

test('pickFile: 名字是纯空白，视为"未填"并预填', () => {
  const p = makePicker();
  p.userTypeName('   ');
  p.pickFile({ name: 'book.txt' });
  deepStrictEqual(p.getName(), 'book');
});

test('pickFile: 重新选择（file=null）不清空名字', () => {
  const p = makePicker();
  p.userTypeName('神秘峡谷');
  p.pickFile(null);
  deepStrictEqual(p.getName(), '神秘峡谷');
  deepStrictEqual(p.getFile(), null);
});

test('pickFile: 自动预填过、切换到另一个文件 → 名字跟新文件更新', () => {
  // 修复前 bug：选 alice.txt 后选 bob.epub，名字仍是 'alice'
  const p = makePicker();
  p.pickFile({ name: 'alice.txt' });
  deepStrictEqual(p.getName(), 'alice');
  p.pickFile({ name: 'bob.epub' });
  deepStrictEqual(p.getName(), 'bob'); // ← 现在会更新
});

test('pickFile: 手动改过名字后再选新文件 → 名字保留手敲值', () => {
  const p = makePicker();
  p.pickFile({ name: 'alice.txt' });      // 自动预填 'alice'
  deepStrictEqual(p.getName(), 'alice');
  p.userTypeName('我改的名');              // 用户编辑 → 进入"手敲"模式
  p.pickFile({ name: 'bob.epub' });       // 再选文件不应覆盖
  deepStrictEqual(p.getName(), '我改的名');
});

test('pickFile: 用户清空名字后再选文件 → 重新自动预填', () => {
  const p = makePicker();
  p.userTypeName('原名');
  p.pickFile({ name: 'foo.txt' });         // '原名' 是手敲的，文件选择不覆盖
  deepStrictEqual(p.getName(), '原名');
  p.userTypeName('');                     // 用户清空
  p.pickFile({ name: 'bar.txt' });         // 空字符串视为"未填"，重新预填
  deepStrictEqual(p.getName(), 'bar');
});
