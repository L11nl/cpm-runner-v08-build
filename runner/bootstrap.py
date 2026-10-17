"""Simple per-session dependency preparation and language dispatch.

Only install a curated set of packages using public registries. No install-time
scripts or source builds. Never install user-supplied requirements options/URLs.
This helper runs inside the *per-account* Railway runner, not Telegram Serverless.
"""
import ast
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

PY_PACKAGES = {
    'requests': 'requests', 'httpx': 'httpx', 'aiohttp': 'aiohttp',
    'bs4': 'beautifulsoup4', 'PIL': 'Pillow', 'yaml': 'PyYAML',
    'dotenv': 'python-dotenv', 'rich': 'rich', 'faker': 'Faker',
    'user_agent': 'user-agent', 'user_agents': 'user-agents',
    'pandas': 'pandas', 'numpy': 'numpy', 'lxml': 'lxml',
    'flask': 'Flask', 'fastapi': 'fastapi', 'uvicorn': 'uvicorn',
    'pydantic': 'pydantic', 'telebot': 'pyTelegramBotAPI',
    'telegram': 'python-telegram-bot', 'telethon': 'Telethon',
    'cryptography': 'cryptography', 'pytz': 'pytz',
    'dateutil': 'python-dateutil', 'openpyxl': 'openpyxl',
    'matplotlib': 'matplotlib', 'tqdm': 'tqdm',
}
JS_PACKAGES = {
    'axios', 'node-fetch', 'dotenv', 'cheerio', 'lodash', 'dayjs',
    'express', 'telegraf', 'grammy', 'uuid', 'zod', 'chalk', 'undici',
    'ws', 'socket.io-client', 'yargs', 'commander', 'form-data',
}
PACKAGE_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?(?:==\d[0-9A-Za-z.+-]*)?$')
JS_IMPORT = re.compile(r'''(?:\brequire\s*\(\s*|\bfrom\s+|\bimport\s*\(\s*|^\s*import\s+)["']([^"']+)["']''', re.M)


def detect_kind(filename: str, source: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == '.js':
        return 'javascript'
    if suffix == '.py':
        return 'python'
    head = source[:600]
    if re.search(r'^#![^\n]*(?:node|deno)', head):
        return 'javascript'
    if re.search(r'^#![^\n]*python', head):
        return 'python'
    if re.search(r'\b(?:console\.log|const\s+\w+\s*=|let\s+\w+\s*=|require\s*\(|module\.exports|function\s+\w+\s*\()', head):
        return 'javascript'
    # Plain .txt defaults to Python (a common way users share pasted code).
    return 'python'


def python_imports(code: str) -> set[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split('.')[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split('.')[0])
    return found


def local_names(root: Path) -> set[str]:
    names = {p.stem for p in root.rglob('*.py') if len(p.relative_to(root).parts) <= 3}
    names.update(p.name for p in root.rglob('*') if p.is_dir() and len(p.relative_to(root).parts) <= 3 and (p/'__init__.py').exists())
    return names


def requirements(root: Path) -> list[str]:
    req = root/'requirements.txt'
    if not req.is_file():
        return []
    if req.stat().st_size > 12_000:
        raise ValueError('requirements.txt كبير جداً')
    selected = []
    safe_names = {x.lower().replace('_','-') for x in PY_PACKAGES.values()}
    for line in req.read_text('utf-8-sig').splitlines():
        item = line.split('#',1)[0].strip()
        if not item:
            continue
        if not PACKAGE_PATTERN.fullmatch(item):
            raise ValueError('requirements.txt يحتوي رابطاً أو خيار تثبيت غير مسموح')
        bare = re.split(r'==|\[',item,1)[0].lower().replace('_','-')
        if bare not in safe_names:
            raise ValueError('مكتبة تحتاج موافقة المالك قبل التثبيت: '+bare)
        selected.append(item)
    return selected[:30]


def show(line: str):
    print(line, flush=True)


def install(cmd: list[str], env: dict):
    # Package registries are accessed only during the install phase;
    # the user's process inherits no API/OAuth secrets from this helper.
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    for line in proc.stdout:
        line = line.strip()
        if line and ('ERROR:' in line or 'error ' in line.lower() or 'Successfully installed' in line):
            show('  '+line[:230])
    result = proc.wait()
    if result:
        raise RuntimeError('فشل تحميل إحدى المكتبات؛ رمز الخروج '+str(result))


def python_setup(root: Path, entry: Path) -> dict:
    show('🔎 جاري فحص مكتبات Python...')
    wanted = requirements(root)
    known_local = local_names(root)
    env = os.environ.copy()
    dest = root/'.cpm_deps'
    dest.mkdir(exist_ok=True)
    # Do not silently fetch lookalike packages from PyPI for arbitrary imports.
    unknown = set()
    for py in list(root.rglob('*.py'))[:100]:
        if '.cpm_deps' in py.parts:
            continue
        try:
            content = py.read_text('utf-8')
        except (OSError, UnicodeError):
            continue
        for imp in python_imports(content):
            if imp in known_local or imp in sys.stdlib_module_names:
                continue
            if imp not in PY_PACKAGES:
                if importlib.util.find_spec(imp) is None:
                    unknown.add(imp)
                continue
            package = PY_PACKAGES[imp]
            if importlib.util.find_spec(imp) is None and package.lower() not in [x.lower() for x in wanted]:
                wanted.append(package)
    if unknown:
        show('⚠️ مكتبات غير معروفة (لن ننزّل أسماء تخمينية): '+', '.join(sorted(unknown)[:10]))
    wanted = list(dict.fromkeys(wanted))
    if wanted:
        show('📦 سيتم تثبيت '+str(len(wanted))+' مكتبات: '+', '.join(wanted[:12]))
        for idx,pkg in enumerate(wanted,1):
            show(f'⏳ [{idx}/{len(wanted)}] تنزيل {pkg} ...')
            installer = [sys.executable,'-m','pip','install','--disable-pip-version-check',
                         '--no-input','--no-cache-dir','--only-binary=:all:',
                         '--retries','1','--timeout','12','--target',str(dest),
                         '--index-url','https://pypi.org/simple',pkg]
            safe_env = {'PATH':env.get('PATH','/usr/local/bin:/usr/bin:/bin'),
                        'HOME':str(root),'LANG':'C.UTF-8','PYTHONIOENCODING':'utf-8',
                        'PIP_CONFIG_FILE':os.devnull,'PIP_DISABLE_PIP_VERSION_CHECK':'1'}
            install(installer,safe_env)
            show(f'✅ [{idx}/{len(wanted)}] اكتمل تثبيت {pkg}')
    else:
        show('✅ كل مكتبات Python المطلوبة متوفرة')
    env['PYTHONPATH'] = str(dest)+os.pathsep+str(root)
    return env


def javascript_setup(root: Path, entry: Path) -> dict:
    show('🔎 جاري فحص مكتبات JavaScript...')
    contents = entry.read_text('utf-8-sig')
    deps = set()
    for mod in JS_IMPORT.findall(contents):
        if mod.startswith(('.', '/', 'node:')):
            continue
        name = mod.split('/')[0]
        if name in JS_PACKAGES:
            deps.add(name)
        elif name not in {'fs','path','os','http','https','url','crypto','buffer','util','stream','events','process','timers','child_process'}:
            show('⚠️ مكتبة JavaScript غير معتمدة للتثبيت التلقائي: '+name[:70])
    package_file=root/'package.json'
    if package_file.is_file():
        if package_file.stat().st_size>12_000:
            raise ValueError('package.json كبير جداً')
        data=json.loads(package_file.read_text('utf-8-sig'))
        for key in ('dependencies',):
            for name,version in data.get(key,{}).items():
                if name not in JS_PACKAGES:
                    show('⚠️ مكتبة في package.json تحتاج موافقة المالك: '+str(name)[:70]);continue
                if not re.fullmatch(r'[~^]?\d+(?:\.\d+){0,2}(?:-[A-Za-z0-9.-]+)?|\*',str(version)):
                    raise ValueError('إصدار المكتبة غير مسموح: '+name)
                deps.add(name)
    elif re.search(r'^\s*(?:import\s+.*from\s+|export\s+)',contents,re.M):
        # Node LTS supports ESM when type=module is set in the project.
        package_file.write_text('{"private":true,"type":"module"}\n')
    if deps:
        pkgs=sorted(deps)
        show('📦 سيتم تثبيت '+str(len(pkgs))+' مكتبات JavaScript: '+', '.join(pkgs))
        for i,name in enumerate(pkgs,1):
            show(f'⏳ [{i}/{len(pkgs)}] في قائمة التنزيل: {name}')
        env = {'PATH':os.getenv('PATH','/usr/bin:/bin'), 'HOME':str(root),
               'npm_config_ignore_scripts':'true','npm_config_audit':'false',
               'npm_config_fund':'false','npm_config_registry':'https://registry.npmjs.org/'}
        install(['npm','install','--prefix',str(root),'--ignore-scripts','--no-audit','--no-fund',
                 '--no-save','--package-lock=false',*pkgs],env)
        for i,name in enumerate(pkgs,1):
            show(f'✅ [{i}/{len(pkgs)}] اكتمل تثبيت {name}')
    else:
        show('✅ ماكو مكتبات JavaScript إضافية مطلوبة')
    return os.environ.copy()


def main():
    if len(sys.argv)!=4:
        raise SystemExit('usage: bootstrap.py DIR ENTRY LANGUAGE')
    root=Path(sys.argv[1]).resolve()
    entry=(root/sys.argv[2]).resolve()
    if not entry.is_file() or not entry.is_relative_to(root):
        raise SystemExit('ملف التشغيل غير صالح')
    language=sys.argv[3]
    try:
        if language=='python':
            env=python_setup(root,entry)
            show('🚀 اكتمل التحضير — بدء Python...')
            os.execve(sys.executable,[sys.executable,'-u',str(entry)],env)
        elif language=='javascript':
            env=javascript_setup(root,entry)
            show('🚀 اكتمل التحضير — بدء Node.js...')
            os.execvpe('node',['node',str(entry)],env)
        else:
            raise ValueError('لغة غير مدعومة')
    except Exception as exc:
        show('❌ تعذر تجهيز المكتبات: '+str(exc)[:200])
        raise SystemExit(2)


if __name__=='__main__':
    main()
