// TypeScript's reader of facts for vibecheck: reads a project with TypeScript's own compiler and
// checker, and prints on stdout, as JSON, the same facts Python's reader gives the checks.
//
//   node reader.cjs <project dir> <source dir>
//
// Names are resolved by the checker, not guessed: `x.m()` reaches the method `m` of the type `x` has.

const ts = require("typescript");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const [projectDir, sourceDir] = process.argv.slice(2).map(p => path.resolve(p));
// A body this small is a one-liner, and two alike are a pattern, not a copy.
const SHAPE_NODES = 40;
const TEST_FILE = /\.(test|spec)\.[cm]?tsx?$/;
const SOURCE_FILE = /\.[cm]?tsx?$/;

// ── the program: the project's own tsconfig, its sources and its tests ──
function program() {
  const configPath = ts.findConfigFile(projectDir, ts.sys.fileExists, "tsconfig.json");
  const config = configPath ? ts.readConfigFile(configPath, ts.sys.readFile).config : {};
  const parsed = ts.parseJsonConfigFileContent(config, ts.sys, projectDir);
  const tests = testsDir() ? walkFiles(testsDir()).filter(f => SOURCE_FILE.test(f)) : [];
  const inSource = parsed.fileNames.filter(f => path.resolve(f).startsWith(sourceDir + path.sep) && !f.endsWith(".d.ts"));
  const options = {...parsed.options, noEmit: true, allowJs: false};
  return {prog: ts.createProgram([...new Set([...inSource, ...walkFiles(sourceDir).filter(f => SOURCE_FILE.test(f) && !f.endsWith(".d.ts")), ...tests])], options), options};
}

function testsDir() {
  for (const name of ["test", "tests", "__tests__"]) {
    const dir = path.join(projectDir, name);
    if (fs.existsSync(dir) && fs.statSync(dir).isDirectory()) return dir;
  }
  return null;
}

function walkFiles(dir) {
  const found = [];
  for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
    if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) found.push(...walkFiles(full));
    else found.push(full);
  }
  return found;
}

const {prog, options} = program();
const checker = prog.getTypeChecker();
const isSource = sf => !sf.isDeclarationFile && path.resolve(sf.fileName).startsWith(sourceDir + path.sep) && !TEST_FILE.test(sf.fileName);
const isTest = sf => !sf.isDeclarationFile && (testsDir() && path.resolve(sf.fileName).startsWith(testsDir() + path.sep) || TEST_FILE.test(sf.fileName));
const sources = prog.getSourceFiles().filter(isSource);
const tests = prog.getSourceFiles().filter(isTest);

// A module's name: its path from the source dir, without the extension, dots for slashes.
const moduleOf = sf => path.relative(sourceDir, path.resolve(sf.fileName)).replace(/\.[cm]?tsx?$/, "").split(path.sep).join(".");
const rel = sf => path.relative(projectDir, path.resolve(sf.fileName));
const lineOf = (sf, pos) => sf.getLineAndCharacterOfPosition(pos).line + 1;
const startLine = node => lineOf(node.getSourceFile(), node.getStart());
const endLine = node => lineOf(node.getSourceFile(), node.getEnd());

// ── symbols: functions, classes and their methods, types, UPPER_CASE constants ──
const symbols = {};
const declToSid = new Map();
const functionNodes = new Map();
const classNodes = new Map();
const exported = node => !!(ts.getCombinedModifierFlags(node) & ts.ModifierFlags.Export) || (ts.isVariableDeclaration(node) && !!(ts.getCombinedModifierFlags(node.parent.parent) & ts.ModifierFlags.Export));
const docOf = node => {
  const tags = ts.getJSDocCommentsAndTags(node);
  const first = tags.length ? tags[0] : null;
  const text = first && typeof first.comment === "string" ? first.comment : first && first.comment ? first.comment.map(c => c.text).join("") : "";
  return text.split("\n")[0].trim();
};
const isFunctionValue = init => init && (ts.isArrowFunction(init) || ts.isFunctionExpression(init));

function addSymbol(sf, name, kind, node, owner, isPrivate, declNodes) {
  const module = moduleOf(sf);
  const id = `${module}:${owner ? owner.split(":")[1] + "." : ""}${name}`;
  if (symbols[id]) return null;
  symbols[id] = {
    id, module, name: owner ? `${owner.split(":")[1]}.${name}` : name, kind, doc: docOf(node),
    line: startLine(node), lines: endLine(node) - startLine(node) + 1, private: isPrivate, owner: owner || null,
    used_by: new Set(), calls: new Set(), call_lines: {},
  };
  for (const d of declNodes) declToSid.set(d, id);
  return id;
}

for (const sf of sources) {
  for (const stmt of sf.statements) {
    if (ts.isFunctionDeclaration(stmt) && stmt.name) {
      const id = addSymbol(sf, stmt.name.text, "function", stmt, null, !exported(stmt), [stmt]);
      if (id) functionNodes.set(id, stmt);
    } else if (ts.isVariableStatement(stmt)) {
      for (const d of stmt.declarationList.declarations) {
        if (!ts.isIdentifier(d.name)) continue;
        if (isFunctionValue(d.initializer)) {
          const id = addSymbol(sf, d.name.text, "function", d, null, !exported(d), [d, d.initializer]);
          if (id) functionNodes.set(id, d.initializer);
        } else if (/^[A-Z][A-Z0-9_]*$/.test(d.name.text)) {
          addSymbol(sf, d.name.text, "constant", d, null, !exported(d), [d]);
        }
      }
    } else if (ts.isClassDeclaration(stmt) && stmt.name) {
      const cls = addSymbol(sf, stmt.name.text, "class", stmt, null, !exported(stmt), [stmt]);
      if (!cls) continue;
      classNodes.set(cls, stmt);
      for (const m of stmt.members) {
        const priv = m.modifiers && m.modifiers.some(x => x.kind === ts.SyntaxKind.PrivateKeyword) || (m.name && ts.isPrivateIdentifier(m.name));
        if ((ts.isMethodDeclaration(m) || ts.isGetAccessor(m) || ts.isSetAccessor(m)) && m.name && m.body) {
          const id = addSymbol(sf, m.name.getText(sf), "method", m, cls, !!priv, [m]);
          if (id) functionNodes.set(id, m);
        } else if (ts.isPropertyDeclaration(m) && m.name && isFunctionValue(m.initializer)) {
          const id = addSymbol(sf, m.name.getText(sf), "method", m, cls, !!priv, [m, m.initializer]);
          if (id) functionNodes.set(id, m.initializer);
        }
      }
    } else if ((ts.isInterfaceDeclaration(stmt) || ts.isTypeAliasDeclaration(stmt) || ts.isEnumDeclaration(stmt)) && stmt.name) {
      addSymbol(sf, stmt.name.text, "type", stmt, null, !exported(stmt), [stmt]);
    }
  }
}

// The symbol of ours a node names, through import aliases.
function resolve(node) {
  let sym = checker.getSymbolAtLocation(node);
  if (!sym) return null;
  if (sym.flags & ts.SymbolFlags.Alias) {
    try { sym = checker.getAliasedSymbol(sym); } catch { return null; }
  }
  for (const d of sym.declarations || []) {
    const sid = declToSid.get(d);
    if (sid) return sid;
  }
  return null;
}

// The scope a node sits in: the innermost function or method of ours, else its class, else the module body.
function scopeOf(node) {
  for (let n = node.parent; n; n = n.parent) {
    const sid = declToSid.get(n);
    if (sid && (symbols[sid].kind === "function" || symbols[sid].kind === "method" || symbols[sid].kind === "class")) return sid;
  }
  return `${moduleOf(node.getSourceFile())}:<module>`;
}

const calleeName = expr => ts.isIdentifier(expr) ? expr.text : ts.isPropertyAccessExpression(expr) ? expr.name.text : ts.isElementAccessExpression(expr) ? null : null;

// ── uses and calls: every name of ours read, every call of ours made, with the line ──
for (const sf of sources) {
  const walk = node => {
    // `this.#take(x)` names `#take` as surely as `take(x)` names `take`.
    if ((ts.isIdentifier(node) || ts.isPrivateIdentifier(node)) && !isDeclarationName(node)) {
      const target = resolve(node);
      const scope = scopeOf(node);
      if (target && target !== scope) symbols[target].used_by.add(scope);
    }
    if (ts.isCallExpression(node) || ts.isNewExpression(node) || ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node) || ts.isDecorator(node)) {
      const callee = ts.isCallExpression(node) || ts.isNewExpression(node) ? node.expression : ts.isDecorator(node) ? node.expression : node.tagName;
      const nameNode = ts.isPropertyAccessExpression(callee) ? callee.name : callee;
      const target = resolve(nameNode);
      const scope = scopeOf(node);
      if (target && symbols[scope] && ["function", "method", "class"].includes(symbols[target].kind)) {
        symbols[scope].calls.add(target);
        (symbols[scope].call_lines[target] ||= new Set()).add(startLine(node));
      }
    }
    ts.forEachChild(node, walk);
  };
  walk(sf);
}

function isDeclarationName(node) {
  const p = node.parent;
  return p && p.name === node && (ts.isFunctionDeclaration(p) || ts.isClassDeclaration(p) || ts.isMethodDeclaration(p) || ts.isVariableDeclaration(p)
    || ts.isInterfaceDeclaration(p) || ts.isTypeAliasDeclaration(p) || ts.isPropertyDeclaration(p) || ts.isParameter(p) || ts.isEnumDeclaration(p)
    || ts.isPropertySignature(p) || ts.isMethodSignature(p) || ts.isGetAccessor(p) || ts.isSetAccessor(p));
}

// ── modules: imports of ours with their line, the names taken, the libraries named ──
const modules = {};
const sourceByFile = new Map(sources.map(sf => [path.resolve(sf.fileName), sf]));
function resolvedModule(spec, sf) {
  const r = ts.resolveModuleName(spec, sf.fileName, options, ts.sys).resolvedModule;
  return r ? sourceByFile.get(path.resolve(r.resolvedFileName)) : null;
}
// An import of types alone is gone once compiled: it ties no module to another at run time.
function typeOnly(node) {
  if (ts.isExportDeclaration(node)) return node.isTypeOnly || (!!node.exportClause && ts.isNamedExports(node.exportClause) && node.exportClause.elements.length > 0 && node.exportClause.elements.every(e => e.isTypeOnly));
  const clause = node.importClause;
  if (!clause) return false;
  if (clause.isTypeOnly) return true;
  const named = clause.namedBindings && ts.isNamedImports(clause.namedBindings) ? clause.namedBindings.elements : [];
  return !clause.name && !(clause.namedBindings && ts.isNamespaceImport(clause.namedBindings)) && named.length > 0 && named.every(e => e.isTypeOnly);
}
const libraryOf = spec => spec.startsWith("@") ? spec.split("/").slice(0, 2).join("/") : spec.split("/")[0];

for (const sf of sources) {
  const m = {path: path.resolve(sf.fileName), lines: sf.getLineAndCharacterOfPosition(sf.getEnd()).line + 1, imports: Object.create(null), taken: Object.create(null), external: Object.create(null)};
  const note = (spec, node, names) => {
    const target = resolvedModule(spec, sf);
    const line = startLine(node);
    if (target && target !== sf) {
      const name = moduleOf(target);
      if (!(name in m.imports)) m.imports[name] = line;
      if (names.length) m.taken[name] = [...new Set([...(m.taken[name] || []), ...names])];
    } else if (!target && !spec.startsWith(".")) {
      const lib = libraryOf(spec);
      if (!(lib in m.external)) m.external[lib] = line;
    }
  };
  const walk = node => {
    if ((ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier) && !typeOnly(node)) {
      const names = [];
      const clause = ts.isImportDeclaration(node) ? node.importClause : null;
      if (clause && clause.name) names.push(clause.name.text);
      const bindings = clause ? clause.namedBindings : node.exportClause;
      if (bindings && (ts.isNamedImports(bindings) || ts.isNamedExports(bindings))) names.push(...bindings.elements.map(e => (e.propertyName || e.name).text));
      note(node.moduleSpecifier.text, node, names);
    } else if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword && node.arguments[0] && ts.isStringLiteral(node.arguments[0])) {
      // A lazy import still ties the modules: it is where a cycle hides.
      note(node.arguments[0].text, node, []);
    }
    ts.forEachChild(node, walk);
  };
  walk(sf);
  modules[moduleOf(sf)] = m;
}

// ── entrypoints: what the package.json's main and exports point at, whole ──
function entryFiles() {
  const pkg = JSON.parse(fs.readFileSync(path.join(projectDir, "package.json"), "utf8"));
  const targets = [];
  const collect = v => { if (typeof v === "string") targets.push(v); else if (v && typeof v === "object") Object.values(v).forEach(collect); };
  collect(pkg.main); collect(pkg.module); collect(pkg.types); collect(pkg.exports);
  return targets.map(t => sourceByFile.get(path.resolve(projectDir, t))).filter(Boolean);
}
const entrypoints = new Set();
for (const sf of entryFiles()) {
  const moduleSymbol = checker.getSymbolAtLocation(sf);
  for (const exp of moduleSymbol ? checker.getExportsOfModule(moduleSymbol) : []) {
    let sym = exp;
    if (sym.flags & ts.SymbolFlags.Alias) { try { sym = checker.getAliasedSymbol(sym); } catch { continue; } }
    for (const d of sym.declarations || []) {
      const sid = declToSid.get(d);
      if (!sid) continue;
      entrypoints.add(sid);
      // An exported class is its public methods too: whoever imports the package calls them.
      for (const [mid, m] of Object.entries(symbols)) if (m.owner === sid && !m.private) entrypoints.add(mid);
    }
  }
}

// ── names read, by the package and by its tests ──
function namesRead(files) {
  const counts = Object.create(null);
  for (const sf of files) {
    const walk = node => {
      if ((ts.isIdentifier(node) || ts.isPrivateIdentifier(node)) && !isDeclarationName(node)) counts[node.text] = (counts[node.text] || 0) + 1;
      ts.forEachChild(node, walk);
    };
    walk(sf);
  }
  return counts;
}

function testUses() {
  // Keyed by names like `constructor`: no prototype to collide with.
  const found = Object.create(null);
  for (const sf of tests) {
    const file = rel(sf);
    const walk = (node, around) => {
      const inner = ts.isFunctionLike(node) && node.body ? [startLine(node), endLine(node)] : around;
      if (ts.isIdentifier(node) && !isDeclarationName(node)) {
        const line = startLine(node);
        (found[node.text] ||= []).push([file, line, inner ? inner[0] : line, inner ? inner[1] : line]);
      }
      ts.forEachChild(node, n => walk(n, inner));
    };
    walk(sf, null);
  }
  for (const k of Object.keys(found)) {
    const seen = new Set();
    found[k] = found[k].filter(u => { const key = u.join(":"); if (seen.has(key)) return false; seen.add(key); return true; }).sort();
  }
  return found;
}

// ── facts: each call, each function, each class ──
const literalKinds = new Set([ts.SyntaxKind.NumericLiteral, ts.SyntaxKind.StringLiteral, ts.SyntaxKind.NoSubstitutionTemplateLiteral,
  ts.SyntaxKind.TrueKeyword, ts.SyntaxKind.FalseKeyword, ts.SyntaxKind.NullKeyword, ts.SyntaxKind.BigIntLiteral]);
const isUndefined = n => ts.isIdentifier(n) && n.text === "undefined";

function argOf(node) {
  const literal = literalKinds.has(node.kind) || isUndefined(node);
  const names = [];
  const collect = n => { if (ts.isIdentifier(n)) names.push(n.text); ts.forEachChild(n, collect); };
  collect(node);
  const base = ts.isPropertyAccessExpression(node) ? node.expression : null;
  return {
    text: node.getText(),
    literal,
    value: literal ? `${ts.SyntaxKind[node.kind]}:${node.getText()}` : "",
    boolean: node.kind === ts.SyntaxKind.TrueKeyword || node.kind === ts.SyntaxKind.FalseKeyword,
    name: ts.isIdentifier(node) && !isUndefined(node) ? node.text : null,
    attr_of: base ? (ts.isIdentifier(base) ? base.text : base.kind === ts.SyntaxKind.ThisKeyword ? "self" : null) : null,
    names,
  };
}

function callOf(node, sf, inTests) {
  let up = node.parent;
  while (up && (ts.isAwaitExpression(up) || ts.isParenthesizedExpression(up) || ts.isVoidExpression(up))) up = up.parent;
  const args = node.arguments ? [...node.arguments] : [];
  return {
    name: calleeName(node.expression) || "",
    file: rel(sf),
    line: startLine(node),
    args: args.filter(a => !ts.isSpreadElement(a)).map(argOf),
    keywords: [],
    spreads: args.some(a => ts.isSpreadElement(a)),
    dropped: !!up && ts.isExpressionStatement(up),
    in_tests: inTests,
  };
}

function paramsOf(fn) {
  return (fn.parameters || []).filter(p => ts.isIdentifier(p.name) && p.name.text !== "this").map(p => p.name.text);
}

function bodyStatements(fn) {
  if (!fn.body) return [];
  return ts.isBlock(fn.body) ? [...fn.body.statements] : [ts.factory.createReturnStatement(fn.body)];
}

// Whether what it returns is worth something: a type that is not void, undefined, never or Promise<void>.
function returnsValue(fn) {
  const sig = checker.getSignatureFromDeclaration(fn);
  if (!sig) return false;
  const t = checker.getReturnTypeOfSignature(sig);
  const empty = x => !!(x.flags & (ts.TypeFlags.Void | ts.TypeFlags.Undefined | ts.TypeFlags.Never | ts.TypeFlags.Any | ts.TypeFlags.Unknown));
  if (empty(t)) return false;
  if (t.isUnion && t.isUnion() && t.types.every(empty)) return false;
  const promised = checker.getPromisedTypeOfPromise ? checker.getPromisedTypeOfPromise(t) : null;
  if (promised && (empty(promised) || (promised.isUnion && promised.isUnion() && promised.types.every(empty)))) return false;
  // `return this` is a fluent interface: its callers chain, or not, by taste.
  if (t.isThisType && t.isThisType()) return false;
  return true;
}

function shapeOf(fn, statements) {
  let count = 0;
  const local = new Map(paramsOf(fn).map((p, i) => [p, `p${i}`]));
  const declare = n => { if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name) && !local.has(n.name.text)) local.set(n.name.text, `v${local.size}`); ts.forEachChild(n, declare); };
  statements.forEach(declare);
  const parts = [];
  const emit = n => {
    count++;
    if (ts.isIdentifier(n)) parts.push(`I:${local.get(n.text) || n.text}`);
    else if (literalKinds.has(n.kind)) parts.push(`L:${n.getText ? safeText(n) : ""}`);
    else parts.push(ts.SyntaxKind[n.kind]);
    ts.forEachChild(n, emit);
    parts.push(")");
  };
  statements.forEach(emit);
  if (count < SHAPE_NODES) return null;
  return crypto.createHash("sha1").update(parts.join(" ")).digest("hex");
}
const safeText = n => { try { return n.getText(); } catch { return n.text || ""; } };

function functionFacts(sid, fn) {
  const sf = fn.getSourceFile();
  const params = paramsOf(fn);
  const loads = Object.create(null);
  const asArgument = new Set();
  const calls = [];
  const receiverReads = Object.create(null);
  const walk = node => {
    if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
      (node.arguments || []).forEach(a => { if (ts.isIdentifier(a)) asArgument.add(a); });
      calls.push(callOf(node, sf, false));
    }
    if (ts.isIdentifier(node) && !isDeclarationName(node) && !(ts.isPropertyAccessExpression(node.parent) && node.parent.name === node)) {
      loads[node.text] = (loads[node.text] || 0) + 1;
    }
    if (ts.isPropertyAccessExpression(node) && !isWrittenTo(node)) {
      const base = node.expression;
      const key = ts.isIdentifier(base) ? base.text : base.kind === ts.SyntaxKind.ThisKeyword ? "self" : null;
      if (key) receiverReads[key] = (receiverReads[key] || 0) + 1;
    }
    ts.forEachChild(node, walk);
  };
  if (fn.body) walk(fn.body);
  (fn.parameters || []).forEach(p => p.initializer && walk(p.initializer));
  const relayed = params.filter(p => loads[p] === 1 && [...asArgument].some(a => a.text === p));
  const statements = bodyStatements(fn);
  const only = statements.length === 1 && (ts.isReturnStatement(statements[0]) || ts.isExpressionStatement(statements[0])) ? unwrap(statements[0].expression) : null;
  const first = statements[0];
  let firstIf = null;
  if (first && ts.isIfStatement(first)) {
    const test = ts.isPrefixUnaryExpression(first.expression) ? first.expression.operand : first.expression;
    if (ts.isIdentifier(test)) firstIf = [test.text, startLine(first), endLine(first)];
  }
  const typed = fn.parameters || [];
  return {
    name: symbols[sid].name.split(".").pop(),
    params,
    positional: params,
    defaults: typed.filter(p => ts.isIdentifier(p.name) && (p.initializer || p.questionToken)).map(p => [p.name.text, p.initializer ? p.initializer.getText() : "undefined"]),
    flags: typed.filter(p => ts.isIdentifier(p.name) && ((p.type && p.type.kind === ts.SyntaxKind.BooleanKeyword) || (p.initializer && (p.initializer.kind === ts.SyntaxKind.TrueKeyword || p.initializer.kind === ts.SyntaxKind.FalseKeyword)))).map(p => p.name.text),
    decorated: !!(ts.canHaveDecorators(fn) && ts.getDecorators(fn) && ts.getDecorators(fn).length),
    stub: !fn.body || (ts.isBlock(fn.body) && (fn.body.statements.length === 0 || (fn.body.statements.length === 1 && ts.isThrowStatement(fn.body.statements[0]) && /not implemented/i.test(fn.body.statements[0].getText())))),
    returns_value: returnsValue(fn),
    loads,
    relayed,
    sole_call: only && (ts.isCallExpression(only) || ts.isNewExpression(only)) ? callOf(only, sf, false) : null,
    first_if: firstIf,
    shape: shapeOf(fn, statements),
    receiver_reads: receiverReads,
    annotations: Object.fromEntries(typed.filter(p => ts.isIdentifier(p.name) && p.type && ts.isTypeReferenceNode(p.type) && ts.isIdentifier(p.type.typeName)).map(p => [p.name.text, p.type.typeName.text])),
    calls,
  };
}
const unwrap = e => { while (e && (ts.isAwaitExpression(e) || ts.isParenthesizedExpression(e))) e = e.expression; return e; };
const isWrittenTo = n => ts.isBinaryExpression(n.parent) && n.parent.left === n && n.parent.operatorToken.kind === ts.SyntaxKind.EqualsToken;

function classFacts(sid, node) {
  const heritage = (node.heritageClauses || []).filter(h => h.token === ts.SyntaxKind.ExtendsKeyword).flatMap(h => h.types);
  const bases = heritage.map(t => ts.isIdentifier(t.expression) ? t.expression.text : ts.isPropertyAccessExpression(t.expression) ? t.expression.name.text : "");
  const libraryBase = heritage.some(t => { const target = resolve(ts.isPropertyAccessExpression(t.expression) ? t.expression.name : t.expression); return !target || (classFactsOf(target) || {}).library_base; });
  return {
    fields: node.members.filter(m => ts.isPropertyDeclaration(m) && m.name && m.type && !isFunctionValue(m.initializer)).map(m => [m.name.getText(), m.type.getText()]),
    bases,
    library_base: libraryBase,
    decorated: !!(ts.getDecorators(node) || []).length,
    abstract: !!(node.modifiers || []).some(x => x.kind === ts.SyntaxKind.AbstractKeyword),
  };
}
const classFactsMemo = new Map();
function classFactsOf(sid) {
  if (!classNodes.has(sid)) return null;
  if (!classFactsMemo.has(sid)) { classFactsMemo.set(sid, {library_base: false}); classFactsMemo.set(sid, classFacts(sid, classNodes.get(sid))); }
  return classFactsMemo.get(sid);
}

// Libraries a module imports by name: `import x from "lib"`, `import {x} from "lib"`, `import * as x from "lib"`.
function outsideNames(sf) {
  const found = Object.create(null);
  for (const stmt of sf.statements) {
    if (!ts.isImportDeclaration(stmt) || !ts.isStringLiteral(stmt.moduleSpecifier)) continue;
    const spec = stmt.moduleSpecifier.text;
    if (spec.startsWith(".") || resolvedModule(spec, sf) || spec.startsWith("node:")) continue;
    const clause = stmt.importClause;
    if (!clause || clause.isTypeOnly) continue;
    if (clause.name) found[clause.name.text] = libraryOf(spec);
    const b = clause.namedBindings;
    if (b && ts.isNamespaceImport(b)) found[b.name.text] = libraryOf(spec);
    if (b && ts.isNamedImports(b)) b.elements.forEach(e => { if (!e.isTypeOnly) found[e.name.text] = libraryOf(spec); });
  }
  return found;
}

function facts() {
  const out = {functions: {}, classes: {}, calls: [], handed: [], handed_in_tests: [], attribute_reads: new Set(), matched_fields: new Set(), listed_names: new Set(), called_names: {}, library_calls: []};
  const handedIn = (sf, into) => {
    const walk = node => {
      if (ts.isIdentifier(node) && !isDeclarationName(node) && !isCallee(node) && !(ts.isPropertyAccessExpression(node.parent) && node.parent.name === node && isCallee(node.parent))) into(node.text);
      ts.forEachChild(node, walk);
    };
    walk(sf);
  };
  for (const sf of sources) {
    const module = moduleOf(sf);
    const called = new Set();
    const outside = outsideNames(sf);
    const walk = (node, inFunction) => {
      if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
        out.calls.push(callOf(node, sf, false));
        const name = calleeName(node.expression);
        if (name) called.add(name);
        const head = ts.isPropertyAccessExpression(node.expression) && ts.isIdentifier(node.expression.expression) ? node.expression.expression.text : ts.isIdentifier(node.expression) ? node.expression.text : null;
        if (inFunction && head && outside[head]) {
          const api = ts.isPropertyAccessExpression(node.expression) ? `${head}.${node.expression.name.text}` : head;
          let up = node.parent;
          while (up && (ts.isAwaitExpression(up) || ts.isParenthesizedExpression(up))) up = up.parent;
          const built = !!up && (ts.isThrowStatement(up) || ts.isReturnStatement(up)) && /^[A-Z]/.test(api.split(".").pop());
          out.library_calls.push({api, module, line: startLine(node), raised_or_returned: built});
        }
      }
      if (ts.isPropertyAccessExpression(node) && !isWrittenTo(node)) out.attribute_reads.add(node.name.text);
      if (ts.isBindingElement(node) && ts.isObjectBindingPattern(node.parent)) out.attribute_reads.add((node.propertyName || node.name).getText());
      if (ts.isArrayLiteralExpression(node)) node.elements.forEach(e => ts.isIdentifier(e) && out.listed_names.add(e.text));
      if (ts.isObjectLiteralExpression(node)) node.properties.forEach(p => { if (ts.isPropertyAssignment(p) && ts.isIdentifier(p.initializer)) out.listed_names.add(p.initializer.text); if (ts.isShorthandPropertyAssignment(p)) out.listed_names.add(p.name.text); });
      if (ts.isSpreadAssignment(node)) called.add("...");
      ts.forEachChild(node, n => walk(n, inFunction || (ts.isFunctionLike(n) && !!n.body)));
    };
    walk(sf, false);
    out.called_names[module] = [...called];
    const seen = new Set();
    handedIn(sf, name => seen.add(name));
    seen.forEach(name => out.handed.push([module, name]));
  }
  const inTests = new Set();
  for (const sf of tests) {
    const walk = node => { if (ts.isCallExpression(node) || ts.isNewExpression(node)) out.calls.push(callOf(node, sf, true)); ts.forEachChild(node, walk); };
    walk(sf);
    handedIn(sf, name => inTests.add(name));
  }
  out.handed_in_tests = [...inTests];
  for (const [sid, fn] of functionNodes) out.functions[sid] = functionFacts(sid, fn);
  for (const sid of classNodes.keys()) out.classes[sid] = classFactsOf(sid);
  out.attribute_reads = [...out.attribute_reads];
  out.matched_fields = [...out.matched_fields];
  out.listed_names = [...out.listed_names];
  return out;
}
const isCallee = n => (ts.isCallExpression(n.parent) || ts.isNewExpression(n.parent)) && n.parent.expression === n;

const pkg = JSON.parse(fs.readFileSync(path.join(projectDir, "package.json"), "utf8"));
const out = {
  name: pkg.name || path.basename(projectDir),
  modules,
  symbols: Object.fromEntries(Object.entries(symbols).map(([id, s]) => [id, {...s, used_by: [...s.used_by], calls: [...s.calls], call_lines: Object.fromEntries(Object.entries(s.call_lines).map(([k, v]) => [k, [...v]]))}])),
  entrypoints: [...entrypoints],
  read_names: namesRead(sources),
  tested_names: namesRead(tests),
  test_uses: testUses(),
  tests_dir: testsDir(),
  facts: facts(),
};
process.stdout.write(JSON.stringify(out));
