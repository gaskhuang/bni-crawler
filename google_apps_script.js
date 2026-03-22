/**
 * BNI 爬蟲授權驗證 - Google Apps Script
 *
 * 部署步驟：
 * 1. 建立 Google Sheet，第一列標題：授權碼 | 客戶名稱 | 狀態 | 到期日
 *    範例資料：
 *    ABC-12345 | 王先生 | 1 | 2026-12-31
 *    XYZ-99999 | 李小姐 | 0 | 2026-06-30
 *    （狀態: 1=啟用, 0=停用；到期日過期也會自動擋）
 *
 * 2. 在 Sheet 上方 → 擴充功能 → Apps Script
 *    把這個檔案的內容貼進去
 *
 * 3. 部署 → 新增部署 → 網頁應用程式
 *    - 執行身分：我
 *    - 誰可以存取：所有人
 *    - 複製部署後的 URL
 *
 * 4. 把 URL 填入客戶的 config.yaml:
 *    license:
 *      api_url: "部署的 URL"
 *      key: "該客戶的授權碼"
 */

function doGet(e) {
  return handleRequest(e);
}

function doPost(e) {
  return handleRequest(e);
}

function handleRequest(e) {
  var key = (e.parameter && e.parameter.key) || '';

  if (!key) {
    return jsonResponse({ valid: false, message: '未提供授權碼' });
  }

  var sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();
  var data = sheet.getDataRange().getValues();

  // 從第二列開始搜尋（第一列是標題）
  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var licenseKey = String(row[0]).trim();
    var customerName = String(row[1]).trim();
    var status = Number(row[2]);
    var expireDate = row[3];

    if (licenseKey === key) {
      // 檢查狀態
      if (status !== 1) {
        logAccess(sheet, i + 1, '拒絕-已停用');
        return jsonResponse({
          valid: false,
          message: '授權已停用，請聯繫管理員',
          customer: customerName
        });
      }

      // 檢查到期日
      if (expireDate) {
        var expire = new Date(expireDate);
        var now = new Date();
        // 設定到期日為當天的 23:59:59
        expire.setHours(23, 59, 59, 999);

        if (now > expire) {
          logAccess(sheet, i + 1, '拒絕-已過期');
          return jsonResponse({
            valid: false,
            message: '授權已過期 (' + Utilities.formatDate(expire, 'Asia/Taipei', 'yyyy-MM-dd') + ')',
            customer: customerName
          });
        }
      }

      // 授權通過
      logAccess(sheet, i + 1, '通過');
      return jsonResponse({
        valid: true,
        message: '授權有效',
        customer: customerName,
        expire_date: expireDate ? Utilities.formatDate(new Date(expireDate), 'Asia/Taipei', 'yyyy-MM-dd') : '永久'
      });
    }
  }

  // 找不到授權碼
  return jsonResponse({ valid: false, message: '無效的授權碼' });
}

function logAccess(sheet, row, result) {
  // 在 E 欄記錄最後存取時間，F 欄記錄結果
  var now = Utilities.formatDate(new Date(), 'Asia/Taipei', 'yyyy-MM-dd HH:mm:ss');

  // 確保標題列有 E、F 欄標題
  if (sheet.getRange(1, 5).getValue() === '') {
    sheet.getRange(1, 5).setValue('最後存取');
    sheet.getRange(1, 6).setValue('存取結果');
  }

  sheet.getRange(row, 5).setValue(now);
  sheet.getRange(row, 6).setValue(result);
}

function jsonResponse(data) {
  return ContentService
    .createTextOutput(JSON.stringify(data))
    .setMimeType(ContentService.MimeType.JSON);
}
