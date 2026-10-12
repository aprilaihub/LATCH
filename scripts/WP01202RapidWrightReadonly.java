// Read-only RapidWright feasibility probe for WP-012-02.
// This program never calls writeCheckpoint or mutates the loaded design.

import com.xilinx.rapidwright.design.Cell;
import com.xilinx.rapidwright.design.Design;
import com.xilinx.rapidwright.design.Net;
import com.xilinx.rapidwright.device.Device;
import com.xilinx.rapidwright.device.Site;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Base64;
import java.util.HashMap;
import java.util.Map;

public class WP01202RapidWrightReadonly {
    private static void emit(String field, long started, String status, String value) {
        long latency = System.currentTimeMillis() - started;
        String encoded = Base64.getEncoder().encodeToString(value.getBytes(StandardCharsets.UTF_8));
        System.out.println("WP01202_RW_FIELD\t" + field + "\t" + latency + "\t" + status + "\t" + encoded);
    }

    private static String escape(String value) {
        return value.replace("\\", "\\\\").replace("\"", "\\\"");
    }

    public static void main(String[] args) throws Exception {
        if (args.length != 1) throw new IllegalArgumentException("one DCP required");
        Path root = Path.of(args[0]).toAbsolutePath().normalize();
        long started = System.currentTimeMillis();
        Design design = Design.readCheckpoint(root.toString());
        emit("read_checkpoint", started, "OK", "loaded");

        Device device = design.getDevice();
        started = System.currentTimeMillis();
        String info = String.format(
            "{\"cells\":%d,\"nets\":%d,\"rows\":%d,\"columns\":%d,\"tiles\":%d,\"sites\":%d}",
            design.getCells().size(), design.getNets().size(), device.getRows(), device.getColumns(),
            device.getAllTiles().size(), device.getAllSites().length);
        emit("design_and_device_info", started, "OK", info);

        started = System.currentTimeMillis();
        Map<String,Integer> types = new HashMap<>();
        int placed = 0;
        int minCol = Integer.MAX_VALUE, maxCol = Integer.MIN_VALUE;
        int minRow = Integer.MAX_VALUE, maxRow = Integer.MIN_VALUE;
        for (Cell cell : design.getCells()) {
            String type = String.valueOf(cell.getType());
            types.put(type, types.getOrDefault(type, 0) + 1);
            if (cell.isPlaced()) {
                placed++;
                Site site = cell.getSite();
                if (site != null) {
                    int col = site.getTile().getColumn();
                    int row = site.getTile().getRow();
                    minCol = Math.min(minCol, col); maxCol = Math.max(maxCol, col);
                    minRow = Math.min(minRow, row); maxRow = Math.max(maxRow, row);
                }
            }
        }
        String placement = String.format(
            "{\"placed_cells\":%d,\"min_col\":%d,\"max_col\":%d,\"min_row\":%d,\"max_row\":%d,\"cell_type_count\":%d}",
            placed, minCol, maxCol, minRow, maxRow, types.size());
        emit("placement_summary", started, "OK", placement);

        started = System.currentTimeMillis();
        int high64 = 0, maximum = 0, routed = 0;
        for (Net net : design.getNets()) {
            int fanout = net.getFanOut();
            if (fanout >= 64) high64++;
            maximum = Math.max(maximum, fanout);
            if (net.hasPIPs()) routed++;
        }
        String nets = String.format(
            "{\"high_fanout_ge64\":%d,\"max_fanout\":%d,\"nets_with_pips\":%d}",
            high64, maximum, routed);
        emit("net_route_summary", started, "OK", nets);

        emit("critical_path_spread", System.currentTimeMillis(), "UNAVAILABLE",
             "requires stable Vivado-to-RapidWright hierarchical object identity mapping");
        emit("critical_net_detour", System.currentTimeMillis(), "UNAVAILABLE",
             "requires stable Vivado-to-RapidWright pin identity mapping");
    }
}
