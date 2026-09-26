import SwiftUI

/// FR-A6 expressions (§10). Driven only by heat.update: `level`, plus `recovered` for 3 s after a
/// verified fix.
enum PetExpression: Equatable {
    case calm, curious, concerned, alarmed, meltdown, recovered

    init(level: Int, recovered: Bool) {
        if recovered {
            self = .recovered
            return
        }
        switch level {
        case ..<1: self = .calm
        case 1: self = .curious
        case 2: self = .concerned
        case 3: self = .alarmed
        default: self = .meltdown
        }
    }
}

/// The horse peeks up from the bottom edge (Snapchat-Bitmoji style). The panel's bottom edge is the
/// "floor": the horse is drawn below it and clipped, and rises further with each level.
/// Eyes, eyebrows and mouth are separate views so each expression swaps them independently.
struct PetView: View {
    let model: PetViewModel
    /// Off for decorative uses (e.g. onboarding), where a heat number means nothing.
    var showsHeatBadge = true

    /// The horse is drawn in 64×84 base points, then scaled up.
    static let scale: CGFloat = 2
    /// Extra headroom above the fully-risen horse for the scanning thought bubble.
    static let panelSize = CGSize(width: 96 * scale, height: 138 * scale)
    private static let baseHorseSize = CGSize(width: 64, height: 84)
    private static var horseSize: CGSize {
        CGSize(width: baseHorseSize.width * scale, height: baseHorseSize.height * scale)
    }

    /// Height of the visible part of the horse above the floor, in screen points.
    static func visibleHeight(level: Int, recovered: Bool = false) -> CGFloat {
        baseVisibleHeight(PetExpression(level: level, recovered: recovered)) * scale
    }

    /// How much of the horse (from its ear tips down, in base points) shows above the floor.
    private static func baseVisibleHeight(_ expression: PetExpression) -> CGFloat {
        switch expression {
        case .calm: return 44       // ears + eyes
        case .curious: return 54
        case .concerned: return 64  // down to the muzzle
        case .alarmed: return 80    // far enough out to show the open "O" mouth
        case .meltdown, .recovered: return baseHorseSize.height  // fully out
        }
    }

    /// Extra room above the horse's visible top that accessories use (the Meltdown sign), so the
    /// bubble opens above them.
    static func accessoryClearance(level: Int, recovered: Bool = false, scanning: Bool = false) -> CGFloat {
        if scanning { return 52 * scale }  // thought bubble
        return PetExpression(level: level, recovered: recovered) == .meltdown ? 16 * scale : 0
    }

    /// "Click me!" when there's an unopened issue (the thinking bubble takes the spot while scanning).
    private var showsClickMe: Bool {
        model.hasUnseenIssue && !model.isScanning && !model.isBubbleOpen
    }

    private var expression: PetExpression {
        PetExpression(level: model.level, recovered: model.isRecovered)
    }

    var body: some View {
        // Continuous motion (breathing, blinking, fidgeting, shaking, hopping) is a function of time;
        // expression changes animate with a spring (see PetPanelController).
        TimelineView(.animation(minimumInterval: 1.0 / 30)) { timeline in
            let motion = HorseMotion(expression: expression, time: timeline.date.timeIntervalSinceReferenceDate)
            HorseFace(expression: expression, motion: motion, character: model.character)
                .frame(width: Self.baseHorseSize.width, height: Self.baseHorseSize.height)
                .overlay {
                    Accessories(expression: expression, unverified: model.unverifiedCount,
                                time: timeline.date.timeIntervalSinceReferenceDate)
                }
                .overlay(alignment: .topTrailing) {
                    if showsHeatBadge { HeatBadge(heat: model.heat).offset(x: 14, y: -2) }
                }
                .overlay {
                    ClickMeCallout(time: timeline.date.timeIntervalSinceReferenceDate)
                        .opacity(showsClickMe ? 1 : 0)
                        .scaleEffect(showsClickMe ? 1 : 0.6, anchor: .bottomLeading)
                        .animation(.spring(response: 0.35, dampingFraction: 0.6), value: showsClickMe)
                }
                .overlay {
                    ThoughtBubble(time: timeline.date.timeIntervalSinceReferenceDate,
                                  hasHorn: model.character == .marley)
                        .opacity(model.isScanning ? 1 : 0)
                        .scaleEffect(model.isScanning ? 1 : 0.6, anchor: .bottomLeading)
                        .animation(.spring(response: 0.35, dampingFraction: 0.7), value: model.isScanning)
                }
                .scaleEffect(x: 1, y: motion.breath, anchor: .bottom)
                .rotationEffect(motion.tilt, anchor: .bottom)
                .offset(x: motion.dx, y: motion.dy)
                .scaleEffect(Self.scale)
                .frame(width: Self.horseSize.width, height: Self.horseSize.height)
                .offset(y: (Self.baseHorseSize.height - Self.baseVisibleHeight(expression)) * Self.scale)
                // Switching characters: sink fully out of view, then come back as the other one.
                .offset(y: model.isCharacterHidden ? Self.horseSize.height + 40 : 0)
                // Rebuild the whole horse per character so nothing carries over between them.
                .id(model.character)
        }
        .frame(width: Self.panelSize.width, height: Self.panelSize.height, alignment: .bottom)
        .clipped()
    }
}

// MARK: - Motion

/// Per-frame motion for an expression (§10 "Action" column), in base points.
private struct HorseMotion {
    var dx: CGFloat = 0
    var dy: CGFloat = 0
    var tilt: Angle = .zero
    var breath: CGFloat = 1
    /// 0 = closed, 1 = open (Calm blinks every 4 s).
    var eyeOpenness: CGFloat = 1
    var spiralSpin: Angle = .zero
    /// Raw time, for effects that run at every level (Marley's sparkles).
    var time: TimeInterval = 0

    init(expression: PetExpression, time t: TimeInterval) {
        time = t
        func wave(_ period: Double) -> CGFloat { CGFloat(sin(2 * .pi * t / period)) }

        switch expression {
        case .calm:
            breath = 1 + 0.015 * wave(3.5)  // gentle breathing
            let phase = t.truncatingRemainder(dividingBy: 4)
            if phase < 0.16 {  // quick blink
                eyeOpenness = max(0.1, CGFloat(abs(phase - 0.08) / 0.08))
            }
        case .curious:
            // Tilt away from the heat badge (top-right) so it stays on screen (FR-A7).
            tilt = .degrees(-7 + 1.5 * Double(wave(2.8)))  // head tilt with a little sway
        case .concerned:
            let phase = t.truncatingRemainder(dividingBy: 2.5)
            if phase < 0.8 { dx = 1.4 * CGFloat(sin(2 * .pi * phase * 4)) }  // fidget in bursts
        case .alarmed:
            dx = 0.8 * wave(0.11)  // trembling
        case .meltdown:
            dx = 3 * wave(0.5)  // steady side-to-side sway
            spiralSpin = .degrees(t.truncatingRemainder(dividingBy: 1.2) / 1.2 * 360)
        case .recovered:
            dy = -8 * abs(CGFloat(sin(.pi * t * 2.2)))  // happy hop
        }
    }
}

// MARK: - Face

/// Coat colours per expression (face, eyebrows and mouth stay dark so the face stays legible).
private struct HorsePalette {
    static let mane = Color(red: 0.22, green: 0.13, blue: 0.08)

    let coat: Color
    let muzzle: Color
    let innerEar: Color

    init(_ expression: PetExpression, character: PetCharacter = .charlie) {
        // (A `where` clause only guards the last pattern, so the Marley check is on both cases.)
        switch expression {
        case .calm where character == .marley, .recovered where character == .marley:  // pearly white unicorn
            coat = Color(red: 0.99, green: 0.97, blue: 0.98)
            muzzle = Color(red: 1.00, green: 0.87, blue: 0.91)
            innerEar = Color(red: 1.00, green: 0.72, blue: 0.84)
        case .calm, .recovered:  // brown
            coat = Color(red: 0.55, green: 0.34, blue: 0.20)
            muzzle = Color(red: 0.80, green: 0.64, blue: 0.50)
            innerEar = Color(red: 0.85, green: 0.62, blue: 0.55)
        case .curious:  // lighter brown
            coat = Color(red: 0.74, green: 0.54, blue: 0.37)
            muzzle = Color(red: 0.91, green: 0.80, blue: 0.68)
            innerEar = Color(red: 0.93, green: 0.72, blue: 0.66)
        case .concerned:  // blue
            coat = Color(red: 0.30, green: 0.50, blue: 0.82)
            muzzle = Color(red: 0.70, green: 0.82, blue: 0.96)
            innerEar = Color(red: 0.62, green: 0.74, blue: 0.95)
        case .alarmed:  // yellow
            coat = Color(red: 0.96, green: 0.78, blue: 0.20)
            muzzle = Color(red: 1.00, green: 0.93, blue: 0.66)
            innerEar = Color(red: 1.00, green: 0.86, blue: 0.55)
        case .meltdown:  // red
            coat = Color(red: 0.86, green: 0.22, blue: 0.20)
            muzzle = Color(red: 0.98, green: 0.68, blue: 0.62)
            innerEar = Color(red: 0.98, green: 0.60, blue: 0.56)
        }
    }
}

/// Drawn in a 64×84 frame with the ear tips at the top edge.
private struct HorseFace: View {
    let expression: PetExpression
    let motion: HorseMotion
    let character: PetCharacter

    private var isMarley: Bool { character == .marley }

    var body: some View {
        let palette = HorsePalette(expression, character: character)
        ZStack {
            // Ears sit behind the head.
            HStack(spacing: 18) {
                Ear(palette: palette, outlined: isMarley)
                Ear(palette: palette, outlined: isMarley)
            }
            .offset(y: -33)

            // Head: tall rounded shape.
            RoundedRectangle(cornerRadius: 22, style: .continuous)
                .fill(palette.coat)
                .overlay {
                    if isMarley {  // soft outline so the white coat reads on light backgrounds
                        RoundedRectangle(cornerRadius: 22, style: .continuous)
                            .strokeBorder(Color(red: 0.93, green: 0.70, blue: 0.84), lineWidth: 1)
                    }
                }
                .frame(width: 46, height: 70)
                .offset(y: 6)

            // Forelock tuft between the ears (Marley: horn, and a rainbow mane framing her face).
            if isMarley {
                UnicornHorn().frame(width: 8, height: 19).offset(y: -39)
                MarleyMane()
            } else {
                Ellipse()
                    .fill(HorsePalette.mane)
                    .frame(width: 20, height: 14)
                    .offset(y: -26)
            }

            // Muzzle with nostrils.
            Ellipse()
                .fill(palette.muzzle)
                .frame(width: 42, height: 28)
                .offset(y: 26)
            HStack(spacing: 14) {
                Nostril()
                Nostril()
            }
            .offset(y: 24)

            Mouth(expression: expression).offset(y: 34)

            if isMarley {
                // Rosy cheeks.
                HStack(spacing: 24) {
                    Ellipse().fill(Color(red: 1, green: 0.45, blue: 0.55).opacity(0.45)).frame(width: 8, height: 5)
                    Ellipse().fill(Color(red: 1, green: 0.45, blue: 0.55).opacity(0.45)).frame(width: 8, height: 5)
                }
                .offset(y: 6)
            }

            HStack(spacing: 18) {
                Eye(expression: expression, isRight: false, openness: motion.eyeOpenness, spin: motion.spiralSpin,
                    lid: palette.coat)
                    .frame(width: 13, height: 13)
                    .overlay(alignment: .top) { if isMarley { Lashes(isRight: false) } }
                Eye(expression: expression, isRight: true, openness: motion.eyeOpenness, spin: motion.spiralSpin,
                    lid: palette.coat)
                    .frame(width: 13, height: 13)
                    .overlay(alignment: .top) { if isMarley { Lashes(isRight: true) } }
            }
            .offset(y: -6)

            HStack(spacing: 18) {
                Eyebrow(expression: expression, isRight: false)
                Eyebrow(expression: expression, isRight: true)
            }
            .offset(y: -16)

            if isMarley {
                Sparkles(time: motion.time)
            }
        }
    }
}

// MARK: - Marley

/// Fuller forelock swept to one side, plus locks falling down both sides of the face.
private struct MarleyMane: View {
    /// Pastel rainbow: pink → lavender → sky blue.
    static let rainbow = [Color(red: 1.00, green: 0.62, blue: 0.80),
                          Color(red: 0.76, green: 0.62, blue: 1.00),
                          Color(red: 0.55, green: 0.80, blue: 1.00)]

    var body: some View {
        ZStack {
            Ellipse()
                .fill(LinearGradient(colors: Self.rainbow, startPoint: .leading, endPoint: .trailing))
                .frame(width: 26, height: 16)
                .rotationEffect(.degrees(-12))
                .offset(x: -2, y: -25)
            HairLock()
                .fill(LinearGradient(colors: Self.rainbow, startPoint: .top, endPoint: .bottom))
                .frame(width: 9, height: 34)
                .scaleEffect(x: -1)
                .offset(x: -23, y: -7)
            HairLock()
                .fill(LinearGradient(colors: Self.rainbow, startPoint: .top, endPoint: .bottom))
                .frame(width: 9, height: 34)
                .offset(x: 23, y: -7)
        }
    }
}

/// A lock of hair: wide at the root, tapering down and curling outward (right) at the tip.
private struct HairLock: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            let w = rect.width, h = rect.height
            p.move(to: CGPoint(x: w * 0.1, y: 0))
            p.addQuadCurve(to: CGPoint(x: w * 0.75, y: h * 0.05), control: CGPoint(x: w * 0.45, y: -h * 0.03))
            // Outer edge flows down and flicks out at the tip.
            p.addCurve(to: CGPoint(x: w, y: h),
                       control1: CGPoint(x: w * 0.95, y: h * 0.4), control2: CGPoint(x: w * 0.6, y: h * 0.8))
            // Inner edge back up, thinner.
            p.addCurve(to: CGPoint(x: w * 0.1, y: 0),
                       control1: CGPoint(x: w * 0.3, y: h * 0.75), control2: CGPoint(x: 0, y: h * 0.35))
            p.closeSubpath()
        }
    }
}

/// Three lashes fanning out from the top-outer edge of each eye.
private struct Lashes: View {
    let isRight: Bool

    var body: some View {
        LashPath()
            .stroke(Color.black, style: StrokeStyle(lineWidth: 1.1, lineCap: .round))
            .frame(width: 13, height: 5)
            .scaleEffect(x: isRight ? 1 : -1)  // mirror so lashes point outward on both eyes
            .offset(y: -3.5)
    }

    private struct LashPath: Shape {
        func path(in rect: CGRect) -> Path {
            Path { p in
                // Drawn for the right eye (outer side = right); mirrored for the left.
                let roots = [(0.55, 0.95), (0.72, 1.0), (0.88, 1.15)]
                let tips = [(0.62, 0.15), (0.86, 0.2), (1.08, 0.45)]
                for (root, tip) in zip(roots, tips) {
                    p.move(to: CGPoint(x: rect.width * root.0, y: rect.height * root.1))
                    p.addLine(to: CGPoint(x: rect.width * tip.0, y: rect.height * tip.1))
                }
            }
        }
    }
}

/// Golden spiral horn.
private struct UnicornHorn: View {
    var body: some View {
        ZStack {
            HornShape()
                .fill(LinearGradient(colors: [Color(red: 1.0, green: 0.95, blue: 0.70),
                                              Color(red: 0.98, green: 0.78, blue: 0.30)],
                                     startPoint: .top, endPoint: .bottom))
            // Spiral grooves.
            HornGrooves()
                .stroke(Color(red: 0.85, green: 0.62, blue: 0.20), style: StrokeStyle(lineWidth: 0.8, lineCap: .round))
                .clipShape(HornShape())
            HornShape().stroke(Color(red: 0.85, green: 0.62, blue: 0.20).opacity(0.6), lineWidth: 0.5)
        }
    }

    private struct HornShape: Shape {
        func path(in rect: CGRect) -> Path {
            Path { p in
                p.move(to: CGPoint(x: rect.midX, y: rect.minY))
                p.addQuadCurve(to: CGPoint(x: rect.maxX, y: rect.maxY),
                               control: CGPoint(x: rect.maxX * 0.8, y: rect.midY))
                p.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
                p.addQuadCurve(to: CGPoint(x: rect.midX, y: rect.minY),
                               control: CGPoint(x: rect.maxX * 0.2, y: rect.midY))
                p.closeSubpath()
            }
        }
    }

    private struct HornGrooves: Shape {
        func path(in rect: CGRect) -> Path {
            Path { p in
                for i in 1...4 {
                    let y = rect.height * CGFloat(i) / 5
                    p.move(to: CGPoint(x: rect.minX, y: y + 2))
                    p.addLine(to: CGPoint(x: rect.maxX, y: y - 2))
                }
            }
        }
    }
}

/// Little four-point stars twinkling around the horn.
private struct Sparkles: View {
    let time: TimeInterval

    private static let spots: [(x: CGFloat, y: CGFloat, size: CGFloat)] =
        [(-15, -40, 5), (15, -46, 6), (20, -33, 4)]

    var body: some View {
        ZStack {
            ForEach(Self.spots.indices, id: \.self) { i in
                let spot = Self.spots[i]
                let twinkle = 0.55 + 0.45 * abs(sin(time * 2 * .pi / 1.6 + Double(i) * 1.3))
                Star()
                    .fill(Color(red: 1.0, green: 0.85, blue: 0.40))
                    .frame(width: spot.size, height: spot.size)
                    .scaleEffect(twinkle)
                    .opacity(twinkle)
                    .offset(x: spot.x, y: spot.y)
            }
        }
    }

    private struct Star: Shape {
        func path(in rect: CGRect) -> Path {
            Path { p in
                let c = CGPoint(x: rect.midX, y: rect.midY)
                let r = rect.width / 2, inner = r * 0.28
                for k in 0..<8 {
                    let angle = Double(k) * .pi / 4 - .pi / 2
                    let radius = k.isMultiple(of: 2) ? r : inner
                    let point = CGPoint(x: c.x + radius * CGFloat(cos(angle)), y: c.y + radius * CGFloat(sin(angle)))
                    k == 0 ? p.move(to: point) : p.addLine(to: point)
                }
                p.closeSubpath()
            }
        }
    }
}

private struct Ear: View {
    let palette: HorsePalette
    var outlined = false

    var body: some View {
        ZStack {
            Triangle().fill(palette.coat).frame(width: 14, height: 18)
            if outlined {
                Triangle().stroke(Color(red: 0.93, green: 0.70, blue: 0.84), lineWidth: 1).frame(width: 14, height: 18)
            }
            Triangle().fill(palette.innerEar).frame(width: 7, height: 10).offset(y: 3)
        }
    }
}

private struct Eye: View {
    let expression: PetExpression
    let isRight: Bool
    let openness: CGFloat
    let spin: Angle
    /// Coat colour, for the Concerned eyelid.
    let lid: Color

    var body: some View {
        switch expression {
        case .recovered:
            // Happy closed eyes: "∩" arcs.
            HappyArc()
                .stroke(HorsePalette.mane, style: StrokeStyle(lineWidth: 2.2, lineCap: .round))
                .frame(width: 12, height: 6)
        case .meltdown:
            ZStack {
                Circle().fill(Color.white).frame(width: 15, height: 15)
                Spiral()
                    .stroke(Color.black, style: StrokeStyle(lineWidth: 1.3, lineCap: .round))
                    .frame(width: 12, height: 12)
                    .rotationEffect(isRight ? spin : -spin)
            }
        default:
            let size = eyeSize
            ZStack {
                Circle().fill(Color.white)
                Circle().fill(Color.black).frame(width: pupilSize, height: pupilSize).offset(look)
                Circle().fill(Color.white).frame(width: 2.5, height: 2.5)
                    .offset(x: look.width + 1.5, y: look.height - 1.5)
            }
            .frame(width: size, height: size)
            .overlay(alignment: .top) {
                // Concerned: heavy upper lid (narrowed, side-eye).
                if expression == .concerned {
                    Rectangle().fill(lid).frame(height: size * 0.42)
                }
            }
            .clipShape(Circle())
            .scaleEffect(x: 1, y: openness)
        }
    }

    private var eyeSize: CGFloat {
        switch expression {
        case .alarmed: return 16
        case .curious: return isRight ? 15.5 : 13  // one eye slightly larger
        default: return 13
        }
    }

    private var pupilSize: CGFloat { expression == .alarmed ? 4.5 : 7 }

    /// Where the pupils look.
    private var look: CGSize {
        switch expression {
        case .curious: return CGSize(width: 1, height: -1.5)
        case .concerned: return CGSize(width: 3, height: 1)  // side-eye
        default: return .zero
        }
    }
}

private struct Eyebrow: View {
    let expression: PetExpression
    let isRight: Bool

    var body: some View {
        Capsule()
            .fill(HorsePalette.mane)
            .frame(width: 10, height: expression == .meltdown ? 3.2 : 2.5)
            .rotationEffect(.degrees(isRight ? -angle : angle))
            .offset(y: lift)
    }

    /// Positive lowers the inner end (knitted); negative raises it (worried/surprised).
    private var angle: Double {
        switch expression {
        case .calm: return 0
        case .curious: return isRight ? 8 : 0
        case .concerned: return 18
        case .alarmed: return -10
        case .meltdown: return -28
        case .recovered: return -6
        }
    }

    /// Vertical offset from the resting position (negative = higher).
    private var lift: CGFloat {
        switch expression {
        case .calm: return 0
        case .curious: return isRight ? -5 : 0  // one raised
        case .concerned: return 1
        case .alarmed: return -5
        case .meltdown: return -6
        case .recovered: return -4
        }
    }
}

private struct Nostril: View {
    var body: some View {
        Ellipse().fill(HorsePalette.mane.opacity(0.8)).frame(width: 5, height: 7)
    }
}

private struct Mouth: View {
    let expression: PetExpression

    var body: some View {
        switch expression {
        case .calm:
            Smile().stroke(HorsePalette.mane, style: stroke).frame(width: 14, height: 5)
        case .curious:
            Capsule().fill(HorsePalette.mane).frame(width: 10, height: 1.8)
        case .concerned:
            Capsule().fill(HorsePalette.mane).frame(width: 16, height: 1.8)
        case .alarmed:
            Ellipse().fill(HorsePalette.mane).frame(width: 8, height: 10)  // open "O"
        case .meltdown:
            Wave().stroke(HorsePalette.mane, style: stroke).frame(width: 18, height: 4)
        case .recovered:
            Grin().fill(HorsePalette.mane).frame(width: 20, height: 9)
        }
    }

    private var stroke: StrokeStyle { StrokeStyle(lineWidth: 1.8, lineCap: .round) }
}

// MARK: - "Click me!" callout

/// Small speech bubble above the head, bobbing gently, when there's an issue the user hasn't
/// opened yet. Same spot as the thinking bubble (they never show together).
private struct ClickMeCallout: View {
    let time: TimeInterval

    var body: some View {
        let bob = CGFloat(sin(time * 2 * .pi / 1.2)) * 1.5
        VStack(alignment: .leading, spacing: 0) {
            Text("Click me!")
                .font(.system(size: 9, weight: .heavy, design: .rounded))
                .foregroundStyle(.white)
                .padding(.horizontal, 7)
                .padding(.vertical, 4)
                .background(Capsule().fill(Color.orange))
                .overlay(Capsule().strokeBorder(Color.white, lineWidth: 1))
            // Tail pointing down at the horse.
            CalloutTail()
                .fill(Color.orange)
                .frame(width: 7, height: 5)
                .padding(.leading, 8)
        }
        .fixedSize()
        .offset(y: bob)
        .position(x: 44, y: -25)
        .frame(width: 64, height: 84)
    }
}

private struct CalloutTail: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            p.move(to: CGPoint(x: rect.minX, y: rect.minY))
            p.addLine(to: CGPoint(x: rect.maxX, y: rect.minY))
            p.addLine(to: CGPoint(x: rect.minX + rect.width * 0.25, y: rect.maxY))
            p.closeSubpath()
        }
    }
}

// MARK: - Scanning thought bubble

/// Shown while the engine checks a reply: a thought bubble above the head with a tiny horse
/// galloping inside. Laid out in the 64×84 horse frame (it sits in the headroom above it).
private struct ThoughtBubble: View {
    let time: TimeInterval
    var hasHorn = false

    var body: some View {
        ZStack {
            // Trail of little thought dots rising from the head.
            Circle().fill(.white).overlay(Circle().stroke(Self.outline, lineWidth: 0.7))
                .frame(width: 3.5, height: 3.5).position(x: 50, y: -12)
            Circle().fill(.white).overlay(Circle().stroke(Self.outline, lineWidth: 0.7))
                .frame(width: 5, height: 5).position(x: 54, y: -19)

            ZStack {
                Cloud().fill(.white)
                Cloud().stroke(Self.outline, lineWidth: 0.8)
                GallopingHorse(time: time, hasHorn: hasHorn).frame(width: 26, height: 17).offset(y: 1)
            }
            .frame(width: 40, height: 28)
            .position(x: 60, y: -35)
        }
        .frame(width: 64, height: 84)
    }

    private static let outline = Color(white: 0.55)
}

/// Rounded cloud: a capsule with a few bumps along the top.
private struct Cloud: Shape {
    func path(in rect: CGRect) -> Path {
        // Union the pieces so the outline is one smooth cloud, not overlapping circles.
        var cloud = Path(roundedRect: rect.insetBy(dx: 0, dy: rect.height * 0.12),
                         cornerRadius: rect.height * 0.38)
        let bump = rect.height * 0.42
        for x in [0.3, 0.52, 0.72] {
            let circle = Path(ellipseIn: CGRect(x: rect.minX + rect.width * x - bump / 2, y: rect.minY - bump * 0.1,
                                                width: bump, height: bump))
            cloud = cloud.union(circle)
        }
        return cloud
    }
}

/// Very simple running horse: bobbing body, alternating legs, streaming tail, scrolling ground.
private struct GallopingHorse: View {
    let time: TimeInterval
    var hasHorn = false

    private static let ink = Color(red: 0.36, green: 0.22, blue: 0.12)

    var body: some View {
        let phase = time * 2 * .pi * 2.6  // strides per second
        let swing = CGFloat(sin(phase))
        let bob = -1.2 * abs(CGFloat(sin(phase)))

        ZStack {
            // Ground moving backwards.
            Path { p in
                p.move(to: CGPoint(x: 0, y: 16.5))
                p.addLine(to: CGPoint(x: 26, y: 16.5))
            }
            .stroke(Self.ink.opacity(0.45),
                    style: StrokeStyle(lineWidth: 0.8, lineCap: .round, dash: [3, 2.5],
                                       dashPhase: CGFloat((time * 14).truncatingRemainder(dividingBy: 5.5))))

            Group {
                // Legs: front pair and back pair swing in opposition.
                leg(x: 16, angle: 28 * swing)
                leg(x: 14.5, angle: -28 * swing)
                leg(x: 8, angle: -28 * swing)
                leg(x: 6.5, angle: 28 * swing)
                // Tail.
                Capsule().fill(Self.ink).frame(width: 1.6, height: 6)
                    .rotationEffect(.degrees(60 + 10 * Double(swing)), anchor: .top)
                    .position(x: 4, y: 7)
                // Body, neck, head.
                Capsule().fill(Self.ink).frame(width: 14, height: 6.5).position(x: 11, y: 9)
                Capsule().fill(Self.ink).frame(width: 4, height: 8)
                    .rotationEffect(.degrees(35)).position(x: 18, y: 5.5)
                Capsule().fill(Self.ink).frame(width: 7, height: 3.6)
                    .rotationEffect(.degrees(25)).position(x: 21.5, y: 3.5)
                // Ear.
                Capsule().fill(Self.ink).frame(width: 1.2, height: 2.6)
                    .rotationEffect(.degrees(15)).position(x: 19.5, y: 1)
                if hasHorn {  // Marley's little horn
                    Capsule().fill(Color(red: 0.98, green: 0.78, blue: 0.30)).frame(width: 1.3, height: 4.5)
                        .rotationEffect(.degrees(40)).position(x: 23.5, y: 0.2)
                }
            }
            .offset(y: bob)
        }
    }

    private func leg(x: CGFloat, angle: CGFloat) -> some View {
        Capsule().fill(Self.ink).frame(width: 1.7, height: 7)
            .rotationEffect(.degrees(Double(angle)), anchor: .top)
            .position(x: x, y: 14.5)
    }
}

// MARK: - Accessories (FR-A7)

/// Laid out in the horse's 64×84 frame (0,0 = top-left, ear tips at the top). The panel leaves 16
/// points either side and 20 above the fully-risen horse, which is where these sit.
private struct Accessories: View {
    let expression: PetExpression
    let unverified: Int
    let time: TimeInterval

    var body: some View {
        ZStack {
            if expression == .curious || expression == .concerned {
                // Inset enough that Curious' head tilt keeps it on screen.
                QuestionBadge(count: unverified).position(x: 10, y: 12)
            }
            if expression == .alarmed || expression == .meltdown {
                RedFlag(time: time).frame(width: 16, height: 54).position(x: -6, y: 32)
                SweatDrop(time: time).position(x: 57, y: 27)
            }
            if expression == .meltdown {
                // Snorting steam from the nostrils, drifting outward.
                Steam(time: time, side: -1).position(x: 25, y: 60)
                Steam(time: time, side: 1).position(x: 39, y: 60)
                StartFreshSign(time: time).position(x: 27, y: -9)
            }
        }
        .frame(width: 64, height: 84)
    }
}

/// "?" with the number of claims the engine couldn't confirm (levels 1–2).
private struct QuestionBadge: View {
    let count: Int

    var body: some View {
        Text(count > 0 ? "? \(count)" : "?")
            .font(.system(size: 10, weight: .heavy, design: .rounded))
            .foregroundStyle(.white)
            .padding(.horizontal, 5)
            .padding(.vertical, 1.5)
            .background(Capsule().fill(Color.orange))
            .overlay(Capsule().strokeBorder(Color.white.opacity(0.8), lineWidth: 0.8))
    }
}

private struct RedFlag: View {
    let time: TimeInterval

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Capsule().fill(Color(white: 0.35)).frame(width: 1.8)
                .frame(maxWidth: .infinity, alignment: .trailing)
            WavingFlag(phase: time * 2 * .pi * 1.6)
                .fill(Color.red)
                .frame(width: 14, height: 10)
                .offset(x: -1.5, y: 1)
        }
    }
}

/// Flag attached at its right edge (the pole) with a travelling wave toward the free left edge.
private struct WavingFlag: Shape {
    var phase: Double

    func path(in rect: CGRect) -> Path {
        Path { p in
            let steps = 16
            func y(_ i: Int, edge: CGFloat) -> CGFloat {
                let fromPole = Double(steps - i) / Double(steps)  // 0 at the pole, 1 at the free end
                return edge + CGFloat(sin(phase + fromPole * 2 * .pi) * 1.4 * fromPole)
            }
            for i in 0...steps {
                let x = rect.minX + rect.width * CGFloat(i) / CGFloat(steps)
                i == 0 ? p.move(to: CGPoint(x: x, y: y(i, edge: rect.minY)))
                    : p.addLine(to: CGPoint(x: x, y: y(i, edge: rect.minY)))
            }
            for i in stride(from: steps, through: 0, by: -1) {
                let x = rect.minX + rect.width * CGFloat(i) / CGFloat(steps)
                p.addLine(to: CGPoint(x: x, y: y(i, edge: rect.maxY)))
            }
            p.closeSubpath()
        }
    }
}

private struct SweatDrop: View {
    let time: TimeInterval

    var body: some View {
        let progress = (time / 1.6).truncatingRemainder(dividingBy: 1)  // drip, fade, repeat
        Teardrop()
            .fill(Color(red: 0.55, green: 0.80, blue: 1.0))
            .overlay(Teardrop().stroke(Color.white.opacity(0.9), lineWidth: 0.6))
            .frame(width: 6, height: 9)
            .offset(y: CGFloat(progress) * 7)
            .opacity(progress < 0.7 ? 1 : (1 - progress) / 0.3)
    }
}

private struct Steam: View {
    let time: TimeInterval
    /// -1 drifts left, 1 drifts right.
    let side: CGFloat

    var body: some View {
        ZStack {
            ForEach(0..<3) { i in
                let p = CGFloat(((time / 1.5) + Double(i) / 3).truncatingRemainder(dividingBy: 1))
                Circle()
                    .fill(Color(white: 0.86))
                    .overlay(Circle().stroke(Color(white: 0.5), lineWidth: 0.6))
                    .frame(width: 3 + p * 7, height: 3 + p * 7)
                    .offset(x: side * p * 28, y: p * 5)
                    .opacity(Double(1 - p) * 0.9)
            }
        }
    }
}

private struct StartFreshSign: View {
    let time: TimeInterval

    var body: some View {
        Text("START FRESH?")
            .font(.system(size: 7, weight: .black, design: .rounded))
            .foregroundStyle(.white)
            .padding(.horizontal, 4)
            .padding(.vertical, 2)
            .background(RoundedRectangle(cornerRadius: 2.5).fill(Color(red: 0.55, green: 0.08, blue: 0.08)))
            .overlay(RoundedRectangle(cornerRadius: 2.5).strokeBorder(Color.white, lineWidth: 0.7))
            .rotationEffect(.degrees(3 * sin(time * 2 * .pi / 1.4)))  // gentle swing
            .fixedSize()
    }
}

// MARK: - Shapes

private struct Teardrop: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            let r = rect.width / 2
            let center = CGPoint(x: rect.midX, y: rect.maxY - r)
            p.move(to: CGPoint(x: rect.midX, y: rect.minY))
            p.addQuadCurve(to: CGPoint(x: rect.maxX, y: center.y), control: CGPoint(x: rect.maxX, y: rect.minY + r))
            p.addArc(center: center, radius: r, startAngle: .degrees(0), endAngle: .degrees(180), clockwise: false)
            p.addQuadCurve(to: CGPoint(x: rect.midX, y: rect.minY), control: CGPoint(x: rect.minX, y: rect.minY + r))
            p.closeSubpath()
        }
    }
}

private struct Smile: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            p.move(to: CGPoint(x: rect.minX, y: rect.minY))
            p.addQuadCurve(to: CGPoint(x: rect.maxX, y: rect.minY), control: CGPoint(x: rect.midX, y: rect.maxY))
        }
    }
}

/// Big open grin: flat top, round bottom.
private struct Grin: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            p.move(to: CGPoint(x: rect.minX, y: rect.minY))
            p.addLine(to: CGPoint(x: rect.maxX, y: rect.minY))
            p.addQuadCurve(to: CGPoint(x: rect.minX, y: rect.minY),
                           control: CGPoint(x: rect.midX, y: rect.maxY + rect.height * 0.8))
            p.closeSubpath()
        }
    }
}

private struct HappyArc: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            p.move(to: CGPoint(x: rect.minX, y: rect.maxY))
            p.addQuadCurve(to: CGPoint(x: rect.maxX, y: rect.maxY),
                           control: CGPoint(x: rect.midX, y: rect.minY - rect.height * 0.6))
        }
    }
}

private struct Wave: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            let steps = 24
            for i in 0...steps {
                let x = rect.minX + rect.width * CGFloat(i) / CGFloat(steps)
                let y = rect.midY + rect.height / 2 * CGFloat(sin(Double(i) / Double(steps) * 3 * 2 * .pi))
                i == 0 ? p.move(to: CGPoint(x: x, y: y)) : p.addLine(to: CGPoint(x: x, y: y))
            }
        }
    }
}

private struct Spiral: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            let turns = 3.0
            let steps = 90
            let maxRadius = min(rect.width, rect.height) / 2
            for i in 0...steps {
                let theta = Double(i) / Double(steps) * turns * 2 * .pi
                let r = maxRadius * CGFloat(Double(i) / Double(steps))
                let point = CGPoint(x: rect.midX + r * CGFloat(cos(theta)), y: rect.midY + r * CGFloat(sin(theta)))
                i == 0 ? p.move(to: point) : p.addLine(to: point)
            }
        }
    }
}

private struct Triangle: Shape {
    func path(in rect: CGRect) -> Path {
        Path { p in
            p.move(to: CGPoint(x: rect.midX, y: rect.minY))
            p.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
            p.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
            p.closeSubpath()
        }
    }
}

/// FR-A7: heat number always visible so color is never the only signal.
private struct HeatBadge: View {
    let heat: Int

    var body: some View {
        Text("\(heat)")
            .font(.system(size: 11, weight: .bold, design: .rounded))
            .foregroundStyle(.white)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Color.black.opacity(0.7)))
    }
}
