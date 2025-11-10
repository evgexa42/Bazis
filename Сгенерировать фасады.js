// Скрипт для автоматического размещения фасадов на основе списка facades_list.txt
// Формат строки: позиция ширина высота количество сторона петли
// Пример: 1 599 760 1 R 2

const listFilePath = '\\\\Server\\базис\\facades_list.txt';
const fragmentsFolder = 'C:\\Users\\жщшо\\Documents\\Bazis\\#_Фрагменты\\Фрагменты элементов мебели\\Фасады';
const gap = 50; // зазор между панелями по оси X

function showAlert(message) {
    if (typeof alert === 'function') {
        alert(message);
        return;
    }
    throw new Error(message);
}

function readTextFile(path) {
    const tryMethods = [];

    tryMethods.push(function () {
        if (typeof system !== 'undefined') {
            if (typeof system.readFile === 'function') return system.readFile(path);
            if (typeof system.ReadFile === 'function') return system.ReadFile(path);
            if (typeof system.readTextFile === 'function') return system.readTextFile(path);
            if (typeof system.ReadTextFile === 'function') return system.ReadTextFile(path);
        }
        return null;
    });

    tryMethods.push(function () {
        if (typeof require === 'function') {
            const fs = require('fs');
            if (fs && typeof fs.readFileSync === 'function') {
                return fs.readFileSync(path, 'utf8');
            }
        }
        return null;
    });

    tryMethods.push(function () {
        if (typeof system !== 'undefined' && typeof system.CreateObject === 'function') {
            const fso = system.CreateObject('Scripting.FileSystemObject');
            if (fso && typeof fso.OpenTextFile === 'function') {
                const file = fso.OpenTextFile(path, 1, false, -1);
                const text = file.ReadAll();
                file.Close();
                return text;
            }
        }
        return null;
    });

    tryMethods.push(function () {
        if (typeof ActiveXObject !== 'undefined') {
            const fso = new ActiveXObject('Scripting.FileSystemObject');
            if (fso && typeof fso.OpenTextFile === 'function') {
                const file = fso.OpenTextFile(path, 1, false, -1);
                const text = file.ReadAll();
                file.Close();
                return text;
            }
        }
        return null;
    });

    tryMethods.push(function () {
        if (typeof WScript !== 'undefined' && typeof WScript.CreateObject === 'function') {
            const fso = WScript.CreateObject('Scripting.FileSystemObject');
            if (fso && typeof fso.OpenTextFile === 'function') {
                const file = fso.OpenTextFile(path, 1, false, -1);
                const text = file.ReadAll();
                file.Close();
                return text;
            }
        }
        return null;
    });

    const errors = [];

    for (let i = 0; i < tryMethods.length; i++) {
        try {
            const result = tryMethods[i]();
            if (result !== null && typeof result !== 'undefined') {
                return String(result);
            }
        } catch (err) {
            errors.push(String(err));
        };
    };

    const details = errors.length ? ('\n' + errors.join('\n')) : '';
    const message = 'Не удалось прочитать файл: ' + path + details;
    showAlert(message);
    throw new Error(message);
}

function normalizeSide(value) {
    if (!value) return null;
    const text = String(value).trim().toLowerCase();
    if (text === 'l' || text === 'left' || text === 'левая' || text === 'л') return 'L';
    if (text === 'r' || text === 'right' || text === 'правая' || text === 'п') return 'R';
    return null;
}

function parseItems(text) {
    const lines = text.split(/\r?\n/);
    const items = [];
    const errors = [];

    for (let i = 0; i < lines.length; i++) {
        const lineNumber = i + 1;
        let line = lines[i];
        if (!line) continue;
        const commentIndexHash = line.indexOf('#');
        const commentIndexSlash = line.indexOf('//');
        const cutIndex = Math.min(
            commentIndexHash === -1 ? line.length : commentIndexHash,
            commentIndexSlash === -1 ? line.length : commentIndexSlash
        );
        line = line.substring(0, cutIndex).trim();
        if (!line) continue;

        const parts = line.split(/[;,\s]+/).filter(Boolean);
        if (parts.length < 5) {
            errors.push('Строка ' + lineNumber + ': ожидается 5 или 6 значений ([позиция] высота ширина количество сторона петли).');
            continue;
        }

        let startIndex = 0;
        let rawPosition = '';

        if (parts.length >= 6) {
            rawPosition = parts[0];
            startIndex = 1;
        }

        const height = Number(parts[startIndex]);
        const width = Number(parts[startIndex + 1]);
        const count = Number(parts[startIndex + 2]);
        const side = normalizeSide(parts[startIndex + 3]);
        const hinges = Number(parts[startIndex + 4]);

        const positionText = String(rawPosition || '').trim();
        const position = positionText !== '' ? positionText : null;

        if (!height || !width || !count || !Number.isFinite(height) || !Number.isFinite(width) || !Number.isFinite(count)) {
            errors.push('Строка ' + lineNumber + ': высота, ширина и количество должны быть числами.');
            continue;
        }
        if (height <= 0 || width <= 0 || count <= 0) {
            errors.push('Строка ' + lineNumber + ': значения должны быть больше нуля.');
            continue;
        }
        if (!side) {
            errors.push('Строка ' + lineNumber + ': укажите сторону (L/R или левая/правая).');
            continue;
        }
        if (!Number.isFinite(hinges) || hinges < 2 || hinges > 6) {
            errors.push('Строка ' + lineNumber + ': количество петель должно быть от 2 до 6.');
            continue;
        }

        items.push({
            pos: position,
            width: width,
            height: height,
            count: Math.round(count),
            side: side,
            hinges: Math.round(hinges)
        });
    }

    if (errors.length) {
        const message = errors.join('\n');
        showAlert(message);
        throw new Error(message);
    }

    return items;
}

const fragmentCache = {};

function getFragment(side, hinges) {
    const key = side + '_' + hinges;
    if (fragmentCache[key]) return fragmentCache[key];

    const baseNames = [
        'Fasad_' + side + '_' + hinges + 'P.fr3d'
    ];

    const attemptedPaths = [];

    for (let i = 0; i < baseNames.length; i++) {
        const path = fragmentsFolder + '\\' + baseNames[i];
        attemptedPaths.push(path);
        const fragment = OpenFurniture(path);
        if (fragment) {
            const info = {
                fragment: fragment,
                path: path,
                attemptedPaths: attemptedPaths
            };
            fragmentCache[key] = info;
            return info;
        }
    }

    return {
        fragment: null,
        path: null,
        attemptedPaths: attemptedPaths
    };
}

function makeFromFragment(fragment) {
    if (!fragment) return { object: null, errors: [] };

    const attempts = [];

    if (typeof fragment.Make === 'function') {
        attempts.push(function () { return fragment.Make(); });

        if (typeof Model !== 'undefined') {
            if (Model && typeof Model.Temp !== 'undefined') {
                attempts.push(function () { return fragment.Make(Model.Temp); });
            }
            if (Model && typeof Model.Unit !== 'undefined') {
                attempts.push(function () { return fragment.Make(Model.Unit); });
            }
            attempts.push(function () { return fragment.Make(Model); });
        }
    }

    if (typeof fragment.Copy === 'function') {
        attempts.push(function () { return fragment.Copy(); });
    }

    if (typeof Model !== 'undefined') {
        if (Model && typeof Model.MakeFurniture === 'function') {
            attempts.push(function () { return Model.MakeFurniture(fragment); });
        }
        if (Model && typeof Model.Make === 'function') {
            attempts.push(function () { return Model.Make(fragment); });
        }
    }

    const errors = [];

    for (let i = 0; i < attempts.length; i++) {
        try {
            const obj = attempts[i]();
            if (obj) {
                return { object: obj, errors: errors };
            }
        } catch (err) {
            errors.push(String(err));
        }
    }

    return { object: null, errors: errors };
}

// === функция вставки фрагмента ===
function placePanel(fragmentInfo, width, height, offsetX, hinges, posNum) {
    const fragment = fragmentInfo && fragmentInfo.fragment ? fragmentInfo.fragment : fragmentInfo;
    const creation = makeFromFragment(fragment);
    const obj = creation.object;

    if (!obj) {
        const extra = fragmentInfo && fragmentInfo.path ? ('\nФрагмент: ' + fragmentInfo.path) : '';
        const details = creation.errors && creation.errors.length ? ('\n' + creation.errors.join('\n')) : '';
        const message = 'Не удалось создать объект из фрагмента.' + extra + details;
        showAlert(message);
        throw new Error(message);
    }

    try { obj.Owner = Model.Temp; } catch (e) {}
    try { obj.Build(); } catch (e) {}

    try { obj.ElasticResize({ x: width, y: height, z: 0 }); } catch (e) {}

    try {
        obj.PositionX = offsetX;
        obj.PositionY = 0;
        obj.PositionZ = 0;
    } catch (e) {}

    try { obj.Name = `Fasad_${width}_${height}_${hinges}`; } catch (e) {}

    // === находим панель внутри фрагмента и задаем ей ArtPos ===
    const artPos = (posNum === undefined || posNum === null) ? '' : String(posNum).trim();

    try {
        obj.forEachPanel(pan => {
            pan.ArtPos = artPos;
        });
    } catch (e) {}
}

// === основной блок ===
const fileContent = readTextFile(listFilePath);
if (!fileContent) {
    const message = 'Файл facades_list.txt пуст или не найден: ' + listFilePath;
    showAlert(message);
    throw new Error(message);
}

const items = parseItems(fileContent);
if (!items.length) {
    const message = 'В файле facades_list.txt нет валидных строк.';
    showAlert(message);
    throw new Error(message);
}

let currentX = 0;

for (let i = 0; i < items.length; i++) {
    const item = items[i];
    const fragmentInfo = getFragment(item.side, item.hinges);
    if (!fragmentInfo || !fragmentInfo.fragment) {
        const attempted = (fragmentInfo && fragmentInfo.attemptedPaths && fragmentInfo.attemptedPaths.length)
            ? ('\nПробовал открыть: ' + fragmentInfo.attemptedPaths.join(', '))
            : '';
        const message = 'Не найден фрагмент для комбинации ' + item.side + ' и ' + item.hinges + ' петель.' + attempted;
        showAlert(message);
        throw new Error(message);
    }

    const quantity = item.count > 0 ? item.count : 1;
    for (let j = 0; j < quantity; j++) {
        placePanel(fragmentInfo, item.width, item.height, currentX, item.hinges, item.pos);
        currentX += item.width + gap;
    }
}

//showAlert('Фасады созданы по списку facades_list.txt.');
