// ==UserScript==
// @name         国際郵便マイページ - 差出人参照番号を一覧表示 + 重量入力→送り状作成
// @namespace    http://tampermonkey.net/
// @version      2.8
// @description  発送予定一覧に差出人参照番号・重量入力・送り状作成ボタンを表示する
// @author       You
// @updateURL    https://cdn.jsdelivr.net/gh/auc-assy-underwear/ems-label-creator@main/int_mypage_ems_creator.user.js
// @downloadURL  https://cdn.jsdelivr.net/gh/auc-assy-underwear/ems-label-creator@main/int_mypage_ems_creator.user.js
// @match        https://www.int-mypage.post.japanpost.jp/mypage/*
// @grant        none
// ==/UserScript==

(function() {
    'use strict';

    const BASE_URL    = 'https://www.int-mypage.post.japanpost.jp/mypage/';
    const DETAIL_URL  = BASE_URL + 'M061200.do';
    const ETC_URL     = BASE_URL + 'M061000.do';  // 発送関連情報編集画面
    const REGIST_URL  = BASE_URL + 'M060900.do';  // 登録確認・完了画面

    window.addEventListener('load', function() {
        const h2 = document.querySelector('h2');
        if (!h2 || h2.textContent.trim() !== '発送予定一覧') return;
        setTimeout(addColumns, 500);
    });

    async function addColumns() {
        const allRows = document.querySelectorAll('tr');
        let headerRow = null;
        let dataRows  = [];

        for (let row of allRows) {
            const text = row.textContent;
            if (text.includes('お届け先国名') && text.includes('発送種別')) {
                headerRow = row;
            } else if (headerRow && row.querySelectorAll('td, th').length >= 7) {
                const cb = row.querySelector('input[type=checkbox]');
                if (cb && cb.value) {
                    dataRows.push({ row, trackingNo: cb.value });
                }
            }
        }

        if (!headerRow || dataRows.length === 0) return;

        const csrfInput = document.querySelector('input[name=csrfToken]');
        if (!csrfInput) return;
        const csrfToken = csrfInput.value;

        headerRow.insertBefore(makeHeaderTh('差出人参照番号'), headerRow.lastElementChild);
        headerRow.insertBefore(makeHeaderTh('重量(g)・送り状作成'), headerRow.lastElementChild);

        for (let item of dataRows) {
            const tdRef = document.createElement('td');
            tdRef.className = 'ce';
            applyTdStyle(tdRef, '（読込中）', '#999');
            tdRef.setAttribute('data-ref-tracking', item.trackingNo);
            item.row.insertBefore(tdRef, item.row.lastElementChild);

            // 重量入力とボタンを1つのセルに上下で配置
            const tdAction = document.createElement('td');
            tdAction.className = 'ce';
            applyTdStyle(tdAction, '', '#333');
            tdAction.style.cssText += ';display:table-cell;';

            const inp = document.createElement('input');
            inp.type        = 'number';
            inp.min         = '1';
            inp.placeholder = 'g';
            inp.style.cssText = 'width:70px;padding:2px 4px;font-size:13px;border:1px solid #ccc;border-radius:3px;text-align:right;display:block;margin:0 auto 4px;';
            inp.setAttribute('data-weight-for', item.trackingNo);

            const btn = document.createElement('input');
            btn.type  = 'button';
            btn.value = '送り状作成';
            btn.style.cssText = 'font-size:12px;padding:3px 8px;cursor:pointer;background:#f60;color:#fff;border:none;border-radius:3px;white-space:nowrap;display:block;margin:0 auto;';
            btn.setAttribute('data-create-for', item.trackingNo);
            btn.addEventListener('click', () => onCreateClick(btn, csrfToken, item.trackingNo));

            tdAction.appendChild(inp);
            tdAction.appendChild(btn);
            item.row.insertBefore(tdAction, item.row.lastElementChild);
        }

        for (let item of dataRows) {
            try {
                const refNo = await fetchReferenceNumber(csrfToken, item.trackingNo);
                const td = document.querySelector(`td[data-ref-tracking="${item.trackingNo}"]`);
                if (td) {
                    td.textContent = refNo || '（未設定）';
                    td.style.color = refNo ? '#333' : '#999';
                }
            } catch (e) {
                const td = document.querySelector(`td[data-ref-tracking="${item.trackingNo}"]`);
                if (td) { td.textContent = 'エラー'; td.style.color = 'red'; }
            }
            await sleep(300);
        }
    }

    async function onCreateClick(btn, csrfToken, trackingNo) {
        const weightInp = document.querySelector(`input[data-weight-for="${trackingNo}"]`);
        const weightVal = weightInp ? weightInp.value.trim() : '';

        if (!weightVal || isNaN(weightVal) || Number(weightVal) <= 0) {
            alert('重量（g）を入力してください。');
            return;
        }

        btn.disabled         = true;
        btn.value            = '処理中…';
        btn.style.background = '#aaa';

        try {
            // ── Step 1: method:info → 詳細画面のフォーム値を収集 ──
            const html1 = await postFetch(DETAIL_URL, {
                'method:info': '',
                cdSel: trackingNo,
                'shipSearchBean.pageNavi.nowPage': '1',
                csrfToken
            });
            const doc1 = parseHtml(html1);
            const fields1 = collectFormFields(doc1);

            // ── Step 2: method:etcChange → 発送関連情報編集画面(M061000.do)を取得 ──
            fields1['method:etcChange'] = '';
            delete fields1['command'];

            const html2 = await postFetch(ETC_URL, fields1);
            const doc2  = parseHtml(html2);
            const fields2 = collectFormFields(doc2);

            // ── 総重量・発送予定日をセット ──
            fields2['shippingBean.totalWeight.value'] = weightVal;
            // 発送予定日を本日に上書き（元の注文が過去日付だとエラーになるため）
            const _t = new Date();
            const _y = _t.getFullYear();
            const _m = String(_t.getMonth() + 1).padStart(2, '0');
            const _d = String(_t.getDate()).padStart(2, '0');
            fields2['shippingBean.sendDate.YMD'] = `${_y}/${_m}/${_d}`;

            // ── Step 3: method:regist → M060900.do（確認画面）──
            // submitCommand('regist') の動作：'command' inputのnameを 'method:regist' にリネームして送信
            fields2['method:regist'] = '';
            delete fields2['command'];

            const html3 = await postFetch(REGIST_URL, fields2);
            const doc3  = parseHtml(html3);

            // M060900.do は「登録内容の確認」画面。エラーチェック後に次のステップへ。
            const errEl3 = doc3.querySelector('.error-message, .errorMessage, #errorMsg');
            if (errEl3) {
                throw new Error('確認画面でエラーが発生しました：\n' + errEl3.textContent.trim());
            }

            // ── Step 4: method:regist → M061000.do（最終登録確定）──
            // 確認画面から「送り状を登録する」ボタンが submitRegist() → submitCommand('regist') を呼び
            // M061000.do に method:regist を送信して登録を確定する
            const fields3 = { 'method:regist': '', csrfToken };

            const html4 = await postFetch(ETC_URL, fields3);
            const doc4  = parseHtml(html4);

            const hasError = doc4.querySelector('.error-message, .errorMessage, #errorMsg');
            if (hasError) {
                throw new Error('登録時にエラーが発生しました。手動でご確認ください。');
            }

            btn.value            = '✓ 登録完了';
            btn.style.background = '#4a4';
            if (weightInp) weightInp.disabled = true;

            if (confirm(`送り状の発送関連情報を登録しました（追跡番号: ${trackingNo}）。\n印刷画面に移動しますか？`)) {
                submitPrint(csrfToken, trackingNo);
            }

        } catch (err) {
            console.error('[送り状作成]', err);
            alert('エラーが発生しました：\n' + err.message);
            btn.disabled         = false;
            btn.value            = '送り状作成';
            btn.style.background = '#f60';
        }
    }

    function collectFormFields(doc) {
        const data = {};
        for (const el of doc.querySelectorAll('input[name], select[name], textarea[name]')) {
            if (el.type === 'button' || el.type === 'submit' || el.type === 'reset') continue;
            if (!(el.name in data)) {
                data[el.name] = el.value || '';
            }
        }
        return data;
    }

    function submitPrint(csrfToken, trackingNo) {
        const form = document.querySelector('form');
        if (!form) return;
        if (typeof alreadySubmit !== 'undefined') alreadySubmit = false;
        const cdSelInput = form.querySelector('input[name=cdSel]');
        if (cdSelInput) cdSelInput.value = trackingNo;
        const cmdInput = form.querySelector('input[name=command]');
        if (cmdInput) {
            cmdInput.name = 'method:print';
            form.submit();
            cmdInput.name = 'command';
        } else {
            const hidden = document.createElement('input');
            hidden.type  = 'hidden';
            hidden.name  = 'method:print';
            hidden.value = '';
            form.appendChild(hidden);
            form.submit();
        }
    }

    async function fetchReferenceNumber(csrfToken, trackingNo) {
        const html = await postFetch(DETAIL_URL, {
            'method:info': '',
            cdSel: trackingNo,
            'shipSearchBean.pageNavi.nowPage': '1',
            csrfToken
        });
        const doc = parseHtml(html);
        for (let th of doc.querySelectorAll('th')) {
            if (th.textContent.trim().includes('差出人参照番号')) {
                const td = th.nextElementSibling;
                if (td) return td.textContent.trim();
            }
        }
        return '';
    }

    async function postFetch(url, params) {
        const body = new URLSearchParams();
        for (const [k, v] of Object.entries(params)) {
            body.append(k, v ?? '');
        }
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body.toString(),
            credentials: 'include'
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.text();
    }

    function parseHtml(html) {
        return new DOMParser().parseFromString(html, 'text/html');
    }

    function makeHeaderTh(text) {
        const th = document.createElement('th');
        th.className = 'ce';
        th.textContent = text;
        th.style.cssText = [
            'background-color:rgb(238,238,238)',
            'border-bottom:1px solid rgb(204,204,204)',
            'border-right:1px solid rgb(204,204,204)',
            'padding:4px',
            'font-size:13px',
            'font-weight:bold',
            'text-align:center',
            'white-space:nowrap',
            'color:rgb(51,51,51)'
        ].join(';');
        return th;
    }

    function applyTdStyle(td, text, color) {
        if (text) td.textContent = text;
        td.style.cssText = [
            'background-color:rgb(255,255,255)',
            'border-bottom:1px solid rgb(204,204,204)',
            'border-right:1px solid rgb(204,204,204)',
            'padding:4px',
            'font-size:13px',
            'text-align:center',
            'white-space:nowrap',
            'vertical-align:middle',
            `color:${color}`
        ].join(';');
    }

    function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }
})();
