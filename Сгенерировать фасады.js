// Скрипт для автоматического размещения фасадов на основе списка facades_list.txt
// Формат строки: ширина высота количество сторона петли
// сторона: L или R (можно также указать left/right/левая/правая в файле)
// петли: число от 2 до 6

const listFilePath = 'C:\\Users\\жщшо\\Documents\\Bazis\\facades_list.txt';
const fragmentsFolder = 'C:\\Users\\жщшо\\Documents\\Bazis\\#_Фрагменты\\Фрагменты элементов мебели';
const gap = 100; // зазор между панелями по оси X

function readTextFile(path) {
    try {
        if (system && typeof system.readFile === 'function') {
            return system.readFile(path);
        }
    } catch (e) {}

    try {
        const fso = new ActiveXObject('Scripting.FileSystemObject');
        const file = fso.OpenTextFile(path, 1, false, -1);
        const content = file.ReadAll();
        file.Close();
        return content;
    } catch (e) {
        system.alert('Не удалось прочитать файл: ' + path + '\n' + e);
        halt;
    }
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
            errors.push('Строка ' + lineNumber + ': ожидается 5 значений.');
            continue;
        }

        const width = Number(parts[0]);
        const height = Number(parts[1]);
        const count = Number(parts[2]);
        const side = normalizeSide(parts[3]);
        const hinges = Number(parts[4]);

        if (!width || !height || !count || !Number.isFinite(width) || !Number.isFinite(height) || !Number.isFinite(count)) {
            errors.push('Строка ' + lineNumber + ': ширина, высота и количество должны быть числами.');
            continue;
        }
        if (width <= 0 || height <= 0 || count <= 0) {
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
            width: width,
            height: height,
            count: Math.round(count),
            side: side,
            hinges: Math.round(hinges)
        });
    }

    if (errors.length) {
        system.alert(errors.join('\n'));
        halt;
    }

    return items;
}

const fragmentCache = {};

function getFragment(side, hinges) {
    const key = side + '_' + hinges;
    if (fragmentCache[key]) return fragmentCache[key];

    const baseNames = [
        'Fasad_' + side + '_' + hinges + 'P.fr3d',
        'Fasad_' + side + '_' + hinges + '.fr3d',
        'Fasdad_' + side + '_' + hinges + 'P.fr3d',
        'Fasdad_' + side + '_' + hinges + '.fr3d',
        'Fasad_' + side + '_' + hinges + '_P.fr3d',
        'Fasdad_' + side + '_' + hinges + '_P.fr3d'
    ];

    for (let i = 0; i < baseNames.length; i++) {
        const path = fragmentsFolder + '\\' + baseNames[i];
        const fragment = OpenFurniture(path);
        if (fragment) {
            fragmentCache[key] = fragment;
            return fragment;
        }
    }

    return null;
}

function placePanel(fragment, width, height, offsetX) {
    const obj = fragment.Make();
    if (!obj) {
        system.alert('Не удалось создать объект из фрагмента.');
        halt;
    }

    try { obj.Owner = Model.Temp; } catch (e) {}
    try { obj.Build(); } catch (e) {}

    try {
        obj.ElasticResize({ x: width, y: height, z: 0 });
    } catch (e) {}

    try {
        obj.PositionX = offsetX;
        obj.PositionY = 0;
        obj.PositionZ = 0;
    } catch (e) {}
}

const fileContent = readTextFile(listFilePath);
if (!fileContent) {
    system.alert('Файл facades_list.txt пуст или не найден: ' + listFilePath);
    halt;
}

const items = parseItems(fileContent);
if (!items.length) {
    system.alert('В файле facades_list.txt нет валидных строк.');
    halt;
}

let currentX = 0;

for (let i = 0; i < items.length; i++) {
    const item = items[i];
    const fragment = getFragment(item.side, item.hinges);
    if (!fragment) {
        system.alert('Не найден фрагмент для комбинации ' + item.side + ' и ' + item.hinges + ' петель.');
        halt;
    }

    const quantity = item.count > 0 ? item.count : 1;
    for (let j = 0; j < quantity; j++) {
        placePanel(fragment, item.width, item.height, currentX);
        currentX += item.width + gap;
    }
}

system.alert('Панели созданы по списку facades_list.txt.');
