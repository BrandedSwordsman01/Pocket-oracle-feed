import com.pocketoracle.kalshibot.KalshiSigner;

import java.nio.file.Files;
import java.nio.file.Paths;

/** Usage: java SignerCheck <pem-file> <message> [pure]  -> prints base64 signature (pure = force pure-Java Ed25519). */
public class SignerCheck {
    public static void main(String[] args) throws Exception {
        String pem = new String(Files.readAllBytes(Paths.get(args[0])), "UTF-8");
        if (args.length > 2 && args[2].equals("pure")) {
            java.lang.reflect.Field f = KalshiSigner.class.getDeclaredField("forcePureEd25519");
            f.setAccessible(true);
            f.setBoolean(null, true);
        }
        KalshiSigner.Key k = KalshiSigner.parsePem(pem);
        System.err.println("type=" + k.type);
        System.out.print(KalshiSigner.sign(k, args[1]));
    }
}
