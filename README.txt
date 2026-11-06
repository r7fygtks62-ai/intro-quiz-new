イントロクイズ 早押しアプリ

【起動（Anaconda / Spyder の場合）】
  Spyderで quiz_server.py を開き、緑の実行ボタンを押す
  コンソールに出るURLを使う（出題者: localhost:8000/host、スマホ: 192.168〜のURL）

【起動（その他）】
  Windows: start.bat をダブルクリック（Pythonが必要）
           開かない場合: フォルダ内で右クリック→「ターミナルで開く」→  python quiz_server.py
  Mac    : start.command をダブルクリック、または  python3 quiz_server.py
  黒い画面に出る「回答者(スマホ)」のURLを、同じWi-Fiのスマホで開く
  （出題者画面のQRコードを読み取ってもOK）
  出題者: http://localhost:8000/host

【曲の追加】
  songs フォルダに 02_intro.mp3 / 02_chorus.mp3 / 02_cover.jpg を入れ、
  songs.json に1行ぶん追加する（今は1曲目だけ登録済み）

【チームの追加・変更】
  teams フォルダの画像と teams.json を編集（現在 A〜G の7チーム）

【操作】  Space: スタート/次の曲   O: 正解   X: 不正解
