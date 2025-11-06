// === путь к фрагменту ===
let fragPath = 'C:\\Users\\жщшо\\Documents\\Bazis\\#_Фрагменты\\Фрагменты элементов мебели\\Fasad_R.fr3d';

// === чтение списка ===
let lines = system.askReadTextFile().split('\n');
let frag = OpenFurniture(fragPath);
if (!frag) { system.alert('Не удалось открыть фрагмент: ' + fragPath); halt; }

let xpos = 0;      // координата X для текущего фрагмента
let gap = 100;     // зазор между фрагментами

for (let i = 0; i < lines.length; i++) {
    let line = lines[i].trim();
    if (!line) continue;

    let parts = line.split(/\s+/);
    if (parts.length < 2) continue;

    let w = Number(parts[0]);
    let h = Number(parts[1]);
    let count = Number(parts[2]) || 1;

    for (let j = 0; j < count; j++) {
        let obj = frag.Make();
        if (!obj) continue;

        try { obj.Owner = Model.Temp; } catch(e){}
        try { obj.Build(); } catch(e){}

        // изменить размеры
        try { obj.ElasticResize({x: w, y: h, z: 0}); } catch(e){}

        // применяем трансформацию для смещения
        try {
            // создаём копию текущей матрицы
            let t = obj.Transform;
            // смещаем по X
            t.tx = xpos;
            t.ty = 0;
            t.tz = 0;
            // применяем обратно
            obj.Transform = t;
        } catch(e){}

        // двигаем позицию для следующего
        xpos += w + gap;
    }
}

alert('Импорт фрагментов завершён.');