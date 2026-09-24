/** @OnlyCurrentDoc */
/** Test only: reads B2 and writes a copy to C2 in the bound QA workbook. */
function verifyCellImage() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  if (book.getId() !== '1VAalOEW3feKbzx6bIwMkx20UnVMaZxWg60odtRQ2C2c') {
    throw new Error('This probe is restricted to the QA workbook.');
  }
  const sheet = book.getSheetByName('Проверка');
  const value = sheet.getRange('B2').getValue();
  if (!value || value.valueType !== SpreadsheetApp.ValueType.IMAGE) {
    throw new Error('B2 must contain an image inserted inside the cell.');
  }
  const url = value.getContentUrl(), host = url.split('/')[2];
  if (url.indexOf('https://') !== 0 || !(host.endsWith('.googleusercontent.com') || host.endsWith('.google.com'))) {
    throw new Error('Unexpected Google image host.');
  }
  const response = UrlFetchApp.fetch(url, {muteHttpExceptions:true});
  if (response.getResponseCode() !== 200) throw new Error('Image download failed.');
  const blob = response.getBlob(), bytes = blob.getBytes();
  if (!bytes.length || bytes.length > 2 * 1024 * 1024) throw new Error('Unexpected image size.');
  const hash = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes)
    .map(b => ('0' + (b & 255).toString(16)).slice(-2)).join('');
  sheet.getRange('C2').setValue(SpreadsheetApp.newCellImage().setSourceUrl(url)
    .setAltTextTitle('Copied test image').build());
  SpreadsheetApp.flush();
  console.log(JSON.stringify({cell:'B2', bytes:bytes.length, mime:blob.getContentType(), sha256:hash,
    copied:sheet.getRange('C2').getValue().valueType === SpreadsheetApp.ValueType.IMAGE}));
}

/** Verified 2026-09-24: image bytes can be written without a public URL. */
function verifyImageBytes() {
  const book = SpreadsheetApp.getActiveSpreadsheet();
  if (book.getId() !== '1VAalOEW3feKbzx6bIwMkx20UnVMaZxWg60odtRQ2C2c') {
    throw new Error('QA workbook only');
  }
  const sheet = book.getSheetByName('Проверка');
  const value = sheet.getRange('B2').getValue();
  const blob = UrlFetchApp.fetch(value.getContentUrl()).getBlob();
  const data = 'data:' + blob.getContentType() + ';base64,' + Utilities.base64Encode(blob.getBytes());
  sheet.getRange('C3').setValue(SpreadsheetApp.newCellImage().setSourceUrl(data)
    .setAltTextTitle('Byte transfer test').build());
  SpreadsheetApp.flush();
  console.log(JSON.stringify({copiedFromBytes:
    sheet.getRange('C3').getValue().valueType === SpreadsheetApp.ValueType.IMAGE}));
}
