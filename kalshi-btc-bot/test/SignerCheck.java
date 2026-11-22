import com.pocketoracle.kalshibot.KalshiSigner;

import java.nio.file.Files;
import java.nio.file.Paths;

/** Usage: java SignerCheck <pem-file> <message>  -> prints base64 signature. */
public class SignerCheck {
    public static void main(String[] args) throws Exception {
        String pem = new String(Files.readAllBytes(Paths.get(args[0])), "UTF-8");
        System.out.print(KalshiSigner.sign(KalshiSigner.parsePem(pem), args[1]));
    }
}
