// Google Apps Script — ADRC SPHERE DUA submission logger
// Deploy as: Execute as "Me", Who has access "Anyone"
// Paste the deployed web app URL into index.html as DUA_ENDPOINT

function doPost(e) {
  try {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getActiveSheet();

    // Write header row on first submission
    if (sheet.getLastRow() === 0) {
      sheet.appendRow([
        'Timestamp (PT)', 'Name', 'Email', 'Institution',
        'Org Type', 'Role / Title', 'Project Title', 'Intended Use'
      ]);
      sheet.getRange(1, 1, 1, 8).setFontWeight('bold');
      sheet.setFrozenRows(1);
    }

    const data = JSON.parse(e.postData.contents);
    const ts   = new Date().toLocaleString('en-US', {timeZone: 'America/Los_Angeles'});

    sheet.appendRow([
      ts,
      data.name    || '',
      data.email   || '',
      data.org     || '',
      data.orgtype || '',
      data.role    || '',
      data.project || '',
      data.use     || ''
    ]);

    return ContentService
      .createTextOutput(JSON.stringify({status: 'ok'}))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({status: 'error', message: err.toString()}))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

// Test by running this manually in the Apps Script editor
function testLog() {
  const fake = {
    postData: {
      contents: JSON.stringify({
        name: 'Jane Smith', email: 'jsmith@stanford.edu',
        org: 'Stanford University', orgtype: 'academic',
        role: 'Postdoc', project: 'AD biomarker methods',
        use: 'Methods development for multi-modal analysis'
      })
    }
  };
  Logger.log(doPost(fake).getContent());
}
